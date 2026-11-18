"""Review utilities for scene character association and unmapped speakers (typer + rich).

Invoked from::

    python -m converter.compose review        # compose CLI action: review scenes/speakers
    mise run compose                          # interactive menu -> Review composites

Standalone commands::

    python -m converter.compose.review compose-review <config>   # review scenes needing character association + unmapped speakers
    python -m converter.compose.review chars-list <config>         # print all known character folder names (source of truth)

Run from the repository root.
"""

from __future__ import annotations

import sys

import typer

from cli import (
    browse_list,
    console,
    counted,
    create_app,
    info,
    is_interactive,
    kv,
    rule,
)

from ..association import CharacterAssociator
from ..config import load_config
from .interactive import review as interactive_review

app = create_app("Review scene character associations and map speakers to characters.")


@app.command()
def chars_list(
    config_path: str = typer.Argument(
        "src/converter/config.json",
        help="Path to the game configuration JSON (default: src/converter/config.json)",
    ),
) -> int:
    """List the character folders available for review."""
    config = load_config(config_path)
    associator = CharacterAssociator(config)
    folders = sorted(associator.known_char_folders())
    rule("Known character folders")
    info(f"Found {counted(len(folders), 'character folder')}.")
    if is_interactive():
        browse_list("Known character folders", folders)
    else:
        for folder in folders:
            console.print(f"  {folder}")
    return 0


@app.command()
def compose_review(
    config_path: str = typer.Argument(
        "src/converter/config.json",
        help="Path to the game configuration JSON (default: src/converter/config.json)",
    ),
) -> int:
    """Associate scene characters and map unmapped speakers.

    Delegates to the interactive review flow in
    :mod:`converter.compose.review.interactive`.
    """
    rule("Compose review")
    kv("Config", config_path)
    return interactive_review(config_path)


def main(argv: list[str] | None = None) -> int:
    """Entry point used by ``python -m converter.compose.review``."""
    return app(args=argv, standalone_mode=False)  # type: ignore[no-any-return]


if __name__ == "__main__":
    sys.exit(main())
