"""Sync exclusion patterns into src/converter/config.json.

Removes any entry from ``rpy_files`` and ``ren_py_files`` that matches a
pattern in ``exclusion.patterns``, then writes the updated config back.

Usage::

    python src/converter/decompile/sync_exclusions.py

Run ``mise run decompile`` first to generate ``config.json``, then set
``exclusion.patterns`` manually (e.g. ``["archive/"]``), and run this
script to drop the matched entries from the matching-pattern lists.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.converter.decompile.decompile import sync_exclusions

CONFIG_PATH = Path("src/converter/config.json")


def main() -> int:
    if not CONFIG_PATH.exists():
        print(f"Config not found: {CONFIG_PATH}")
        print("Run 'mise run decompile' first to generate it.")
        return 1

    result = sync_exclusions(CONFIG_PATH)
    if not result:
        return 1

    print(
        f"rpy_files: {result['rpy_before']} -> {result['rpy_after']}; "
        f"ren_py_files: {result['ren_py_before']} -> {result['ren_py_after']}"
    )
    print(f"Config: {CONFIG_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
