"""Sync exclusion patterns into src/converter/config.json (typer + rich).

Removes any entry from ``rpy_files`` and ``ren_py_files`` that matches a
pattern in ``exclusion.patterns``, then writes the updated config back.

Usage::

    python -m converter.decompile.sync-exclusions

Run ``mise run decompile`` first to generate ``config.json``, then set
``exclusion.patterns`` manually (e.g. ``["archive/"]``), and run this
script to drop the matched entries from the matching-pattern lists.
"""

from __future__ import annotations

from pathlib import Path

from cli import create_app, error, info, kv, rule

app = create_app("Sync exclusion patterns into src/converter/config.json.")

CONFIG_PATH = Path("src/converter/config.json")


@app.command()
def main() -> int:
    """Sync exclusion patterns into config.json."""
    rule("Sync exclusions")
    kv("Config", str(CONFIG_PATH))

    if not CONFIG_PATH.exists():
        error(f"Config not found: {CONFIG_PATH}")
        info("Run 'mise run decompile' first to generate it.")
        return 1

    from converter.decompile.exclusions import sync_exclusions

    result = sync_exclusions(CONFIG_PATH)
    if not result:
        error("Failed to sync exclusions.")
        return 1

    rule("Sync complete")
    kv("rpy_files", f"{result['rpy_before']} -> {result['rpy_after']}")
    kv("ren_py_files", f"{result['ren_py_before']} -> {result['ren_py_after']}")
    kv("Config", str(CONFIG_PATH))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(app())
