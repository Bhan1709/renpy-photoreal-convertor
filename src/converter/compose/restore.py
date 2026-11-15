"""Restore logic for the compose subpackage.

Restores Ren'Py ``.rpy`` source files that were modified by the apply pass,
by re-extracting them from the game's ``archive.rpa``.

This provides a safety net: if the apply pass makes unwanted changes to live
scripts, running restore brings them back to their original archived state.
"""

from __future__ import annotations

from pathlib import Path

from .config import GameConfig
from .errors import ComposeError


class RestoreError(ComposeError):
    """Raised when restoring .rpy files from the archive fails."""


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
    config: GameConfig,
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
        config: The :class:`GameConfig` describing the game.
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
    archive = archive_path or find_archive(config.game_root)
    if archive is None:
        raise RestoreError(
            f"No archive.rpa found in {config.game_root}; cannot restore .rpy files"
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

            target = config.game_root / filename
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
    config: GameConfig,
    manifest_path: Path | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, str]:
    """Restore only the .rpy files that the apply pass touched.

    Reads the adoption report / manifest to determine which scripts had
    replacements applied, then restores only those files from the archive.

    Args:
        config: The :class:`GameConfig` describing the game.
        manifest_path: Path to the manifest JSON.  Defaults to
            ``config.resolved_paths()["manifest_path"]``.
        dry_run: If True, report what would be restored without writing.

    Returns:
        A dict mapping restored relative paths to a status string.
    """
    import json

    mp = manifest_path or config.resolved_paths()["manifest_path"]
    if not mp.is_file():
        raise RestoreError(f"Manifest not found: {mp}")

    data = json.loads(mp.read_text(encoding="utf-8"))

    # Collect all unique script paths that had usage locations or replacements.
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

    return restore_rpy_files(config, only_files=only_files, dry_run=dry_run)
