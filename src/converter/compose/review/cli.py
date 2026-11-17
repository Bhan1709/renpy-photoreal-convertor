"""Review utilities for scene character association and unmapped speakers (typer + rich).

Mise tasks:

    mise run compose-review      # Interactive review of scenes + unmapped speakers
    mise run chars-list          # Fetch all character names (source of truth)

Run from the repository root.
"""

from __future__ import annotations

import sys

import typer

from cli import console, create_app, info, kv, rule

from ..association import CharacterAssociator
from ..config import load_config
from .interactive import review as interactive_review

app = create_app(
    "Review utilities for scene character association and unmapped speakers."
)


@app.command()
def chars_list(
    config_path: str = typer.Argument(
        "src/converter/config.json",
        help="Path to the GameConfig JSON (default: src/converter/config.json)",
    ),
) -> int:
    """Print all known character folder names (source of truth)."""
    config = load_config(config_path)
    associator = CharacterAssociator(config)
    folders = sorted(associator.known_char_folders())
    rule("Character folders")
    info(f"Total: {len(folders)}")
    for folder in folders:
        console.print(f"  {folder}")
    return 0


@app.command()
def compose_review(
    config_path: str = typer.Argument(
        "src/converter/config.json",
        help="Path to the GameConfig JSON (default: src/converter/config.json)",
    ),
) -> int:
    """Review scenes needing character association and unmapped speakers.

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
