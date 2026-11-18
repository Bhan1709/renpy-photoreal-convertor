"""Sync exclusion patterns into ``src/converter/config.json`` (typer + rich).

Usage::

    python -m converter.decompile.sync_exclusions_cli

The compose entry point calls :func:`run_exclusion_sync` only after the user
selects a compose-producing action (``compose`` or ``clean-compose``). Run this
module directly to sync without composing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from cli import confirm, create_app, error, info, is_interactive, kv, rule, success

from .exclusions import prompt_exclusion_patterns, sync_exclusions

app = create_app("Sync exclusion patterns into src/converter/config.json.")

CONFIG_PATH = Path("src/converter/config.json")


def run_exclusion_sync(
    config_path: Path = CONFIG_PATH,
    *,
    prompt: bool = True,
) -> int:
    """Optionally add new exclusion patterns, then sync ``config.json``.

    Shared by the standalone CLI and the compose entry point.
    With ``prompt=True`` (the default) the user is asked whether to add script
    or image exclusions first; the question is skipped when there is no
    interactive terminal.

    Returns a process exit code: ``0`` on success, ``1`` on failure.
    """
    rule("Exclusion check")
    kv("Config", str(config_path))

    if not config_path.is_file():
        error(f"Config not found: {config_path}")
        info("Run 'mise run decompile' first to generate it.")
        return 1

    if prompt and is_interactive():
        answer = confirm(
            "Would you like to add or update exclusion patterns now?",
            default=False,
        )
        if answer is None:
            info("Exclusion check cancelled. No exclusions were changed.")
            return 1
        if answer:
            prompt_exclusion_patterns(config_path)

    result = sync_exclusions(config_path)
    if not result:
        return 1

    kv("Script files", f"{result['rpy_before']} -> {result['rpy_after']}")
    kv("Ren'Py files", f"{result['ren_py_before']} -> {result['ren_py_after']}")
    success("Exclusions synced to config.json.")
    return 0


@app.command()
def main() -> None:
    """Sync exclusion patterns into config.json."""
    raise typer.Exit(run_exclusion_sync(CONFIG_PATH, prompt=True))


if __name__ == "__main__":
    sys.exit(app())
