""".rpyc decompilation via rpa-toolkit."""

from __future__ import annotations

from pathlib import Path


def decompile_rpyc_files(game_folder: Path) -> int:
    r"""Decompile every ``.rpyc`` under ``game_folder`` to a sibling ``.rpy``.

    Uses ``rpa-toolkit``.  Returns the number of files successfully
    decompiled.
    """
    from rpatool import decompile_rpyc_file

    rpyc_files = sorted(game_folder.rglob("*.rpyc"))
    if not rpyc_files:
        print(f"No .rpyc files found in {game_folder}")
        return 0

    print(f"Decompiling {len(rpyc_files)} .rpyc file(s) in {game_folder}")
    ok = err = fail = 0
    for rpyc_file in rpyc_files:
        try:
            output_path = rpyc_file.with_suffix(".rpy")
            result = decompile_rpyc_file(
                rpyc_file, output_path=output_path, overwrite=True
            )
            if result.success:
                print(f"  [OK] Decompiled: {rpyc_file.name} -> {output_path.name}")
                ok += 1
            else:
                print(f"  [ERR] Error decompiling {rpyc_file.name}: {result.error}")
                err += 1
        except Exception as e:  # noqa: BLE001
            print(f"  [XX] Failed to decompile {rpyc_file.name}: {e}")
            fail += 1

    print(f"  Decompile done: {ok} ok, {err} error(s), {fail} failed")
    return ok
