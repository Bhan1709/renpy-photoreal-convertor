"""Restore logic: revert modified Ren'Py ``.rpy`` source files from an archive.

Restores Ren'Py ``.rpy`` source files that were modified by a compose/apply
pass, by re-extracting them from the game's ``archive.rpa``.

This subpackage is intentionally independent of :mod:`converter.compose` — it
only needs a ``game_root`` (the Ren'Py ``\\game`` folder) and, optionally, a
manifest path.  This makes it reusable by the decompile/pipeline layers as
well as by compose. Archive members, manifest script references, and destructive
restore roots are containment-checked before live files are modified.

Usage::

    from converter.restore import restore_rpy_files, restore_from_manifest

    # Restore every .rpy stored in archive.rpa back into game_root.
    results = restore_rpy_files(Path("src/renpy_game/MyGame/game"))

    # Restore only the scripts the apply pass touched (per manifest).
    results = restore_from_manifest(
        Path("src/renpy_game/MyGame/game"),
        Path("composites/reports/manifest_index.json"),
    )

    # Undo a full decompile: remove the extracted game folder and config.json.
    results = restore_decompile()
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from cli import loading

from ..utility.manifest_bundle import (
    ManifestBundlePathError,
    game_script_lock,
    manifest_bundle_lock,
    recover_manifest_bundle,
    validate_manifest_index_path,
)
from ..utility.manifest_paths import (
    ManifestPathError,
    resolve_category_manifest_paths,
    verify_category_manifest_digests,
)
from ..utility.safe_paths import (
    UnsafePathError,
    game_script_relative_path,
    resolve_under_root,
)
from .errors import RestoreError


def find_archive(game_root: Path) -> Path | None:
    """Locate ``archive.rpa`` inside ``game_root``.

    Returns the path to the archive, or ``None`` if not found.
    """
    archive = game_root / "archive.rpa"
    if archive.is_file():
        return archive
    # Also check for any .rpa file in the game root as a fallback.
    rpa_files = list(game_root.glob("*.rpa"))
    if rpa_files:
        return rpa_files[0]
    return None


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """Atomically replace one restored script with *data*."""
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(raw_temporary)
    os.close(descriptor)
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def restore_rpy_files(
    game_root: Path,
    *,
    archive_path: Path | None = None,
    only_files: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, str]:
    """Restore archived ``.rpy`` files while holding the game-script lock."""
    with game_script_lock(game_root):
        return _restore_rpy_files_unlocked(
            game_root,
            archive_path=archive_path,
            only_files=only_files,
            dry_run=dry_run,
        )


def _restore_rpy_files_unlocked(
    game_root: Path,
    *,
    archive_path: Path | None = None,
    only_files: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, str]:
    """Restore ``.rpy`` files from the game's archive into ``game_root``.

    Re-extracts every ``.rpy`` file stored in ``archive.rpa`` and writes it
    back to ``game_root``, overwriting any modifications made by the apply
    pass (or any other process).

    Args:
        game_root: The Ren'Py ``\\game`` folder (contains the ``.rpy`` scripts
            and, typically, an ``archive.rpa``).
        archive_path: Explicit path to the archive.  Defaults to
            ``game_root / archive.rpa``.
        only_files: If provided, only restore these relative paths (e.g.
            ``["script.rpy", "camp.rpy"]``).  Otherwise all ``.rpy`` files
            in the archive are restored.
        dry_run: If True, report what would be restored without writing.

    Returns:
        A dict mapping restored relative paths to a status string
        (``"restored"``, ``"would_restore"``, or ``"skipped"``).
    """
    archive = archive_path or find_archive(game_root)
    if archive is None:
        raise RestoreError(
            f"No archive.rpa found in {game_root}; cannot restore .rpy files"
        )

    try:
        from rpatool.archive import RenPyArchive
    except ImportError as exc:
        raise RestoreError(
            "rpa-toolkit is required to restore .rpy files from archive"
        ) from exc

    results: dict[str, str] = {}
    restore_message = (
        "Inspecting scripts in the game archive..."
        if dry_run
        else "Restoring scripts from the game archive..."
    )
    with (
        RenPyArchive(archive, verbose=False) as rpa,
        loading(restore_message),
    ):
        candidates: list[tuple[str, Path]] = []
        for filename in rpa.list():
            if not filename.endswith(".rpy"):
                continue
            if only_files is not None and filename not in only_files:
                continue
            try:
                target = resolve_under_root(
                    game_root,
                    filename,
                    field_name="archive script path",
                )
            except UnsafePathError as exc:
                raise RestoreError(str(exc)) from exc
            candidates.append((filename, target))

        for filename, target in candidates:
            if dry_run:
                results[filename] = "would_restore"
                continue

            try:
                data = rpa.read(filename)
                _atomic_write_bytes(target, data)
                results[filename] = "restored"
            except Exception as exc:  # noqa: BLE001
                results[filename] = f"skipped: {exc}"

    return results


def restore_from_manifest(
    game_root: Path,
    manifest_path: str | Path | None = None,
    *,
    manifest_index_path: str | Path | None = None,
    report_dir: str | Path | None = None,
    dry_run: bool = False,
) -> dict[str, str]:
    """Restore only the .rpy files that a compose/apply pass touched.

    Both the split manifest index and the legacy monolithic manifest are
    supported.  Split category references are resolved relative to the index
    directory; categories omitted from a partial index fall back to the
    canonical filenames in ``report_dir``.

    Args:
        game_root: The Ren'Py ``\\game`` folder.
        manifest_path: Legacy manifest path or manifest index path.
        manifest_index_path: Explicit path to ``manifest_index.json``.
        report_dir: Explicit report directory used to locate the index and
            legacy manifest fallback.
        dry_run: If True, report what would be restored without writing.

    Returns:
        A dict mapping restored relative paths to a status string.
    """
    explicit_manifest = manifest_index_path is not None or manifest_path is not None
    if manifest_index_path is not None:
        index_path = Path(manifest_index_path)
    elif manifest_path is not None:
        index_path = Path(manifest_path)
    else:
        category_report_dir = (
            Path(report_dir)
            if report_dir is not None
            else game_root.parent / "composites" / "reports"
        )
        index_path = category_report_dir / "manifest_index.json"

    if manifest_index_path is not None or manifest_path is not None:
        category_report_dir = (
            Path(report_dir) if report_dir is not None else index_path.parent
        )

    with (
        manifest_bundle_lock(index_path, report_dir=category_report_dir),
        game_script_lock(game_root),
    ):
        if not explicit_manifest and not index_path.is_file():
            legacy_manifest = category_report_dir / "manifest.json"
            if legacy_manifest.is_file():
                index_path = legacy_manifest
        try:
            validate_manifest_index_path(index_path, category_report_dir)
        except ManifestBundlePathError as exc:
            raise RestoreError(str(exc)) from exc
        try:
            recover_manifest_bundle(index_path, category_report_dir)
        except (OSError, RuntimeError) as exc:
            raise RestoreError(f"Could not recover manifest bundle: {exc}") from exc
        return _restore_from_manifest_unlocked(
            game_root,
            index_path,
            category_report_dir,
            dry_run=dry_run,
        )


def _restore_from_manifest_unlocked(
    game_root: Path,
    index_path: Path,
    category_report_dir: Path,
    *,
    dry_run: bool,
) -> dict[str, str]:
    """Restore from a manifest while the caller holds the bundle lock."""
    try:
        validate_manifest_index_path(index_path, category_report_dir)
    except ManifestBundlePathError as exc:
        raise RestoreError(str(exc)) from exc
    if not index_path.is_file():
        raise RestoreError(f"Manifest not found: {index_path}")

    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RestoreError(f"Could not read manifest {index_path}: {exc}") from exc

    if not isinstance(index, dict):
        raise RestoreError(f"Manifest must contain an object: {index_path}")

    is_split_bundle = any(
        marker in index
        for marker in ("categories", "category_digests", "bundle_version")
    )
    records: list[dict] = []
    if not is_split_bundle:
        # A legacy monolithic manifest is self-contained.  Do not merge
        # stale split category files that happen to live beside it.
        legacy_records = index.get("composites", [])
        if not isinstance(legacy_records, list):
            raise RestoreError("Manifest 'composites' must be a list")
        for record in legacy_records:
            if not isinstance(record, dict):
                raise RestoreError("Manifest contains an invalid composite record")
        records.extend(legacy_records)
    else:
        try:
            category_paths = resolve_category_manifest_paths(
                index,
                index_path,
                category_report_dir,
            )
            verify_category_manifest_digests(
                index,
                category_paths,
                report_dir=category_report_dir,
                manifest_index_path=index_path,
            )
        except ManifestPathError as exc:
            raise RestoreError(str(exc)) from exc

        for category_path in category_paths.values():
            try:
                category_data = json.loads(category_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RestoreError(
                    f"Could not read manifest category file {category_path}: {exc}"
                ) from exc
            if not isinstance(category_data, dict):
                raise RestoreError(
                    f"Manifest category file must contain an object: {category_path}"
                )
            composites = category_data.get("composites", [])
            if not isinstance(composites, list):
                raise RestoreError(
                    f"Manifest category 'composites' must be a list: {category_path}"
                )
            for composite in composites:
                if not isinstance(composite, dict):
                    raise RestoreError(
                        f"Manifest category contains an invalid composite: {category_path}"
                    )
            records.extend(composites)

    scripts_to_restore: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        for location in record.get("usage_locations", []):
            if not isinstance(location, dict):
                continue
            script = str(location.get("script") or "")
            if not script:
                continue
            try:
                relative = game_script_relative_path(game_root, script)
            except UnsafePathError as exc:
                raise RestoreError(str(exc)) from exc
            scripts_to_restore.add(relative.as_posix())

    only_files = sorted(scripts_to_restore)
    if not only_files:
        return {}

    return restore_rpy_files(game_root, only_files=only_files, dry_run=dry_run)


# --------------------------------------------------------------------------- #
# Decompile restore
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG_PATH: Path = (
    Path(__file__).resolve().parents[3] / "src/converter/config.json"
)
DEFAULT_LIVE_ROOT: Path = Path(__file__).resolve().parents[2] / "renpy_game"


def restore_decompile(
    config_path: str | Path | None = None,
    *,
    allowed_root: str | Path | None = None,
    dry_run: bool = False,
) -> dict[str, str]:
    """Undo a full decompile: remove the extracted game folder and config.json.

    Reads the decompile-generated ``config.json`` to discover the
    ``game_folder`` that was extracted, then removes that directory tree and
    deletes ``config.json`` itself.

    Args:
        config_path: Path to the decompile-generated ``config.json``.
            Defaults to ``src/converter/config.json`` relative to the project
            root.
        allowed_root: Independent root that may contain the extracted game
            folder.  Defaults to ``src/renpy_game``.
        dry_run: If True, report what would be removed without writing.

    Returns:
        A dict mapping removed paths to a status string
        (``"removed"``, ``"would_remove"``, or ``"skipped"``).
    """
    cp = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH
    if not cp.is_absolute():
        cp = Path(__file__).resolve().parents[3] / cp

    results: dict[str, str] = {}

    if not cp.is_file():
        results[str(cp)] = "skipped: config.json not found"
        return results

    # Read config.json to discover the game_folder before deleting anything.
    try:
        data = json.loads(cp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        results[str(cp)] = f"skipped: cannot read config.json ({exc})"
        return results

    game_folder = data.get("game_folder")
    game_root: Path | None = None
    if game_folder:
        gp = Path(game_folder)
        if not gp.is_absolute():
            gp = cp.parent.parent.parent / gp  # project root / game_folder
        allowed = Path(allowed_root) if allowed_root is not None else DEFAULT_LIVE_ROOT
        allowed_path = allowed.resolve()
        lexical_root = gp
        resolved_root = gp.resolve()
        if (
            resolved_root == allowed_path
            or not resolved_root.is_relative_to(allowed_path)
            or lexical_root.is_symlink()
        ):
            raise RestoreError(
                f"Refusing to remove game folder outside {allowed_path}: {gp}"
            )
        game_root = resolved_root

    # Remove the extracted game folder.
    if game_root is not None and game_root.exists():
        if dry_run:
            results[str(game_root)] = "would_remove"
        else:
            try:
                with loading("Removing decompile artifacts..."):
                    shutil.rmtree(game_root)
                results[str(game_root)] = "removed"
            except OSError as exc:
                results[str(game_root)] = f"skipped: {exc}"
    elif game_root is not None:
        results[str(game_root)] = "skipped: game folder not found"

    # Delete config.json itself.
    if dry_run:
        results[str(cp)] = "would_remove"
    else:
        try:
            cp.unlink()
            results[str(cp)] = "removed"
        except OSError as exc:
            results[str(cp)] = f"skipped: {exc}"

    return results


# --------------------------------------------------------------------------- #
# Compose restore (recover the live game folder from the original source)     #
# --------------------------------------------------------------------------- #

COMPOSITES_DIR_NAME = "composites"
COMPOSITE_DECLARATION_FILE = "composite_images.rpy"


def restore_compose_from_source(
    config_path: str | Path | None = None,
    *,
    allowed_live_root: str | Path | None = None,
    allowed_source_root: str | Path | None = None,
    dry_run: bool = False,
) -> dict[str, str]:
    r"""Restore the live ``game_folder`` from the original source, keeping composites.

    Reads ``config.json`` to discover ``source_path`` (the original game folder
    where the ``\game`` was found) and ``game_folder`` (the live working copy
    that compose/apply modify).  Copies every entry from ``source_path/game``
    into ``game_folder``, overwriting any files the apply pass changed, while
    **skipping** the live ``game_folder/composites`` directory so the generated
    PNGs and report manifests are preserved for a future re-apply.

    The apply-generated ``game_folder/composite_images.rpy`` declarations file
    is removed (the apply pass regenerates it).

    Args:
        config_path: Path to ``config.json``.  Defaults to
            ``src/converter/config.json`` relative to the project root.
        allowed_live_root: Independent root that may contain the live game
            folder.  Defaults to ``src/renpy_game``.
        allowed_source_root: Independent root that may contain the source
            game folder.  Defaults to ``src/renpy_game``.
        dry_run: If True, report what would be done without writing.

    Returns:
        A dict mapping affected paths to a status string
        (``"restored"``, ``"would_overwrite"``, ``"removed"``, ``"would_remove"``,
        ``"skipped: ..."``).
    """
    cp = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH
    if not cp.is_absolute():
        cp = Path(__file__).resolve().parents[3] / cp

    results: dict[str, str] = {}

    if not cp.is_file():
        results[str(cp)] = "skipped: config.json not found"
        return results

    # Read config.json to discover source_path and game_folder before writing.
    try:
        data = json.loads(cp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        results[str(cp)] = f"skipped: cannot read config.json ({exc})"
        return results

    source_path = data.get("source_path")
    game_folder = data.get("game_folder")
    if not source_path or not game_folder:
        results[str(cp)] = "skipped: config.json missing 'source_path' or 'game_folder'"
        return results

    sp = Path(source_path)
    if not sp.is_absolute():
        sp = cp.parent.parent.parent / sp  # project root / source_path
    gp = Path(game_folder)
    if not gp.is_absolute():
        gp = cp.parent.parent.parent / gp  # project root / game_folder

    live_allowed = (
        Path(allowed_live_root)
        if allowed_live_root is not None
        else DEFAULT_LIVE_ROOT
    ).resolve()
    source_allowed = (
        Path(allowed_source_root)
        if allowed_source_root is not None
        else DEFAULT_LIVE_ROOT
    ).resolve()
    lexical_source_game = sp / "game"
    source_game = lexical_source_game.resolve()
    lexical_live_root = gp
    live_root = gp.resolve()
    if (
        live_root == live_allowed
        or not live_root.is_relative_to(live_allowed)
        or lexical_live_root.is_symlink()
    ):
        raise RestoreError(
            f"Refusing to restore game files outside {live_allowed}: {gp}"
        )
    if (
        source_game == source_allowed
        or not source_game.is_relative_to(source_allowed)
        or lexical_source_game.is_symlink()
    ):
        raise RestoreError(
            f"Refusing to restore from a source game outside {source_allowed}: "
            f"{lexical_source_game}"
        )
    if (
        live_root.is_relative_to(source_game)
        or source_game.is_relative_to(live_root)
    ):
        raise RestoreError(
            "Refusing to restore a game folder from an overlapping source path: "
            f"{source_game} -> {live_root}"
        )
    if not source_game.is_dir():
        results[str(source_game)] = (
            "skipped: source game folder not found; run decompile first"
        )
        return results

    copy_entries: list[tuple[Path, Path]] = []
    for entry in sorted(source_game.iterdir()):
        if entry.name == COMPOSITES_DIR_NAME:
            continue
        target = live_root / entry.name
        if target.is_symlink():
            raise RestoreError(
                f"Refusing to overwrite a symlinked live file: {target}"
            )
        resolved_target = target.resolve()
        if not resolved_target.is_relative_to(live_root):
            raise RestoreError(
                f"Live restore target escapes the game root: {target}"
            )
        resolved_source = entry.resolve()
        if not resolved_source.is_relative_to(source_game):
            raise RestoreError(
                f"Source restore entry escapes the source game root: {entry}"
            )
        if entry.is_dir() and any(
            child.is_symlink() for child in entry.rglob("*")
        ):
            raise RestoreError(
                f"Source restore directory contains a symlink: {entry}"
            )
        copy_entries.append((entry, target))

    with game_script_lock(live_root):
        if not live_root.exists():
            if dry_run:
                results[str(live_root)] = "would_create"
            else:
                try:
                    live_root.mkdir(parents=True, exist_ok=True)
                    results[str(live_root)] = "created"
                except OSError as exc:
                    results[str(live_root)] = f"skipped: {exc}"
                    return results
        for entry in sorted(source_game.iterdir()):
            if entry.name == COMPOSITES_DIR_NAME:
                results[str(entry)] = "skipped: preserving live composites"

        # Copy every entry from source/game into game_folder, overwriting, but
        # never touch the live compose output (PNGs + reports under composites/).
        restore_message = (
            "Inspecting source game files..."
            if dry_run
            else "Restoring game files from the source game..."
        )
        with loading(restore_message):
            for entry, target in copy_entries:
                if dry_run:
                    results[str(target)] = f"would_overwrite ({entry.name})"
                    continue
                try:
                    if entry.is_dir() and not entry.is_symlink():
                        if target.exists():
                            shutil.rmtree(target)
                        shutil.copytree(entry, target)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(entry, target)
                    results[str(target)] = "restored"
                except OSError as exc:
                    results[str(target)] = f"skipped: {exc}"

        # Remove the apply-generated composite declarations file (regenerated by
        # the apply pass; not part of the original source).
        declaration = live_root / COMPOSITE_DECLARATION_FILE
        if declaration.is_file():
            if dry_run:
                results[str(declaration)] = "would_remove"
            else:
                try:
                    declaration.unlink()
                    results[str(declaration)] = "removed"
                except OSError as exc:
                    results[str(declaration)] = f"skipped: {exc}"
        elif dry_run:
            results[str(declaration)] = "would_remove: not present"

        return results
