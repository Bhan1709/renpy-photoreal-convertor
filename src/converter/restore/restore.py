"""Restore logic: revert modified Ren'Py ``.rpy`` source files from an archive.

Restores Ren'Py ``.rpy`` source files that were modified by a compose/apply
pass, by re-extracting them from the game's ``archive.rpa``.

This subpackage is intentionally independent of :mod:`converter.compose` — it
only needs a ``game_root`` (the Ren'Py ``\\game`` folder) and, optionally, a
manifest path.  This makes it reusable by the decompile/pipeline layers as
well as by compose.

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
import shutil
from pathlib import Path

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


def restore_rpy_files(
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
    with RenPyArchive(archive, verbose=False) as rpa:
        for filename in rpa.list():
            if not filename.endswith(".rpy"):
                continue
            if only_files is not None and filename not in only_files:
                continue

            target = game_root / filename
            if dry_run:
                results[filename] = "would_restore"
                continue

            try:
                data = rpa.read(filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                results[filename] = "restored"
            except Exception as exc:  # noqa: BLE001
                results[filename] = f"skipped: {exc}"

    return results


def restore_from_manifest(
    game_root: Path,
    manifest_path: str | Path | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, str]:
    """Restore only the .rpy files that a compose/apply pass touched.

    Reads the manifest JSON to determine which scripts had usage locations
    (and therefore potential replacements), then restores only those files
    from the archive.

    Args:
        game_root: The Ren'Py ``\\game`` folder.
        manifest_path: Path to the manifest JSON.  Defaults to
            ``game_root.parent / "composites" / "reports" / "manifest.json"``.
        dry_run: If True, report what would be restored without writing.

    Returns:
        A dict mapping restored relative paths to a status string.
    """
    mp = Path(manifest_path) if manifest_path is not None else None
    if mp is None:
        mp = game_root.parent / "composites" / "reports" / "manifest.json"
    if not mp.is_file():
        raise RestoreError(f"Manifest not found: {mp}")

    data = json.loads(mp.read_text(encoding="utf-8"))

    # Collect all unique script paths that had usage locations.
    scripts_to_restore: set[str] = set()
    for record in data.get("composites", []):
        for loc in record.get("usage_locations", []):
            script = loc.get("script", "")
            if script:
                scripts_to_restore.add(script)

    # The manifest doesn't directly record which scripts were rewritten, but
    # every script with a usage location had potential replacements.  Restore
    # all of them to be safe.
    only_files = sorted(scripts_to_restore)
    if not only_files:
        return {}

    return restore_rpy_files(game_root, only_files=only_files, dry_run=dry_run)


# --------------------------------------------------------------------------- #
# Decompile restore
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG_PATH: Path = Path(__file__).resolve().parents[3] / "src/converter/config.json"


def restore_decompile(
    config_path: str | Path | None = None,
    *,
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
        game_root = gp

    # Remove the extracted game folder.
    if game_root is not None and game_root.exists():
        if dry_run:
            results[str(game_root)] = "would_remove"
        else:
            try:
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
        results[str(cp)] = (
            "skipped: config.json missing 'source_path' or 'game_folder'"
        )
        return results

    sp = Path(source_path)
    if not sp.is_absolute():
        sp = cp.parent.parent.parent / sp  # project root / source_path
    gp = Path(game_folder)
    if not gp.is_absolute():
        gp = cp.parent.parent.parent / gp  # project root / game_folder

    source_game = sp / "game"
    if not source_game.is_dir():
        results[str(source_game)] = (
            "skipped: source game folder not found; run decompile first"
        )
        return results

    if not gp.exists():
        if dry_run:
            results[str(gp)] = "would_create"
        else:
            try:
                gp.mkdir(parents=True, exist_ok=True)
                results[str(gp)] = "created"
            except OSError as exc:
                results[str(gp)] = f"skipped: {exc}"
                return results

    # Copy every entry from source/game into game_folder, overwriting, but never
    # touch the live compose output (PNGs + reports under composites/).
    for entry in sorted(source_game.iterdir()):
        if entry.name == COMPOSITES_DIR_NAME:
            results[str(entry)] = "skipped: preserving live composites"
            continue
        target = gp / entry.name
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
    declaration = gp / COMPOSITE_DECLARATION_FILE
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
