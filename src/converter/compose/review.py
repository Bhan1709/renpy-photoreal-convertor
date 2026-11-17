"""Review utilities for scene character association and unmapped speakers.

Mise tasks:

    mise run compose-review      # Interactive review of scenes + unmapped speakers
    mise run chars-list          # Fetch all character names (source of truth)

Run from the repository root.
"""

from __future__ import annotations

import sys

from .character_associator import CharacterAssociator
from .config import load_config
from .interactive_review import review as interactive_review


def chars_list(config_path: str = "src/converter/config.json") -> int:
    """Print all known character folder names (source of truth)."""
    config = load_config(config_path)
    associator = CharacterAssociator(config)
    folders = sorted(associator.known_char_folders())
    print(f"Character folders ({len(folders)}):")
    for folder in folders:
        print(f"  {folder}")
    return 0


def compose_review(config_path: str = "src/converter/config.json") -> int:
    """Review scenes needing character association and unmapped speakers.

    Delegates to the interactive review flow in
    :mod:`converter.compose.interactive_review`.
    """
    return interactive_review(config_path)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    if not argv:
        print("Usage: python -m converter.compose.review <chars-list|compose-review> [config.json]")
        return 1

    command = argv[0]
    config_path = argv[1] if len(argv) > 1 else "src/converter/config.json"

    if command == "chars-list":
        return chars_list(config_path)
    if command == "compose-review":
        return compose_review(config_path)

    print(f"Unknown command: {command}")
    print("Usage: python -m converter.compose.review <chars-list|compose-review> [config.json]")
    return 1


if __name__ == "__main__":
    sys.exit(main())
