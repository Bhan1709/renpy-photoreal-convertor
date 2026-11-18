""".rpyc decompilation via rpa-toolkit."""

from __future__ import annotations

from pathlib import Path

from cli import counted, error, info, loading, success


def decompile_rpyc_files(game_folder: Path) -> int:
    r"""Decompile every ``.rpyc`` under ``game_folder`` to a sibling ``.rpy``.

    Uses ``rpa-toolkit``.  Returns the number of files successfully
    decompiled.
    """
    from rpatool import decompile_rpyc_file

    rpyc_files = sorted(game_folder.rglob("*.rpyc"))
    if not rpyc_files:
        info(f"No .rpyc files found in {game_folder}")
        return 0

    info(f"Decompiling {counted(len(rpyc_files), '.rpyc file')} in {game_folder}")
    ok = err = fail = 0
    for rpyc_file in rpyc_files:
        try:
            output_path = rpyc_file.with_suffix(".rpy")
            with loading(f"Decompiling {rpyc_file.name}..."):
                result = decompile_rpyc_file(
                    rpyc_file, output_path=output_path, overwrite=True
                )
            if result.success:
                success(f"Decompiled: {rpyc_file.name} -> {output_path.name}")
                ok += 1
            else:
                error(f"Could not decompile {rpyc_file.name}: {result.error}")
                err += 1
        except Exception as e:  # noqa: BLE001
            error(f"Failed to decompile {rpyc_file.name}: {e}")
            fail += 1

    success(
        f"Decompile complete: {ok} succeeded, {counted(err, 'error')}, {fail} failed."
    )
    return ok
