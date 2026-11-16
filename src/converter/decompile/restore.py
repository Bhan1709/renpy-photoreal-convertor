"""Restore: undo a previous decompile of a game folder."""

from __future__ import annotations

import shutil
from pathlib import Path


def restore_decompile(
    target_folder: Path, *, dry_run: bool = False
) -> dict[str, str]:
    r"""Undo a previous decompile of a game folder.

    Removes generated ``.rpy`` files that have a ``.rpyc`` sibling, and
    removes extracted archive directories (sibling dirs named after ``.rpa``
    stems), restoring the folder to its pre-decompile state.

    Returns a dict mapping path -> status
    (``"removed"``, ``"would_remove"``, or ``"skipped: ..."``).
    """
    results: dict[str, str] = {}

    # Remove generated .rpy files that have a corresponding .rpyc sibling.
    for rpyc_file in sorted(target_folder.rglob("*.rpyc")):
        rpy_file = rpyc_file.with_suffix(".rpy")
        if rpy_file.is_file():
            key = str(rpy_file)
            if dry_run:
                results[key] = "would_remove"
            else:
                try:
                    rpy_file.unlink()
                    results[key] = "removed"
                except Exception as exc:  # noqa: BLE001
                    results[key] = f"skipped: {exc}"

    # Remove extracted archive directories (sibling dirs named after .rpa stems).
    for rpa_file in sorted(target_folder.rglob("*.rpa")):
        extract_dir = rpa_file.parent / rpa_file.stem
        if extract_dir.is_dir():
            key = str(extract_dir)
            if dry_run:
                results[key] = "would_remove"
            else:
                try:
                    shutil.rmtree(extract_dir)
                    results[key] = "removed"
                except Exception as exc:  # noqa: BLE001
                    results[key] = f"skipped: {exc}"

    return results
