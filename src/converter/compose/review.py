"""Review utilities for scene character association and unmapped speakers.

Mise tasks:

    mise run compose-review      # Review scenes needing character association + unmapped speakers
    mise run chars-list          # Fetch all character names (source of truth)

Run from the repository root.
"""

from __future__ import annotations

import sys

from .character_associator import CharacterAssociator
from .config import load_config
from .manifest import ManifestWriter


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
    """Review scenes needing character association and unmapped speakers."""
    config = load_config(config_path)
    pending = ManifestWriter.load_pending_review(config)

    if not pending:
        print("No pending scene reviews — all scene composites have character associations.")
        return 0

    scenes = pending.get("scenes", [])
    unmapped = pending.get("unmapped_speakers", [])
    pending_count = pending.get("pending_reviews", len(scenes))
    print(f"=== Pending Scene Reviews ({pending_count} scene(s) without character association) ===\n")
    for entry in scenes:
        print(f"  Composite: {entry['composite_path']}")
        print(f"  Name:      {entry['name']}")
        print(f"  Defined:   {entry['definition']['script']}:{entry['definition']['line']}")
        print("  Usage locations:")
        for loc in entry["usage_locations"]:
            print(f"    - {loc['script']}:{loc['line']}  {loc['statement']}")
        print()

    if unmapped:
        print(f"=== Unmapped Speakers ({len(unmapped)} speaker(s) with no character map) ===\n")
        print("These speakers appear in scene dialog but could not be mapped to a char folder.\n"
              "Add a ``Character()`` declaration (e.g. ``define h = Character(\"Hazel\", image=\"ha\")``)\n"
              "or a matching folder under ``chars/`` to resolve them.\n")
        for entry in unmapped:
            print(f"  Speaker: {entry['speaker']}")
            print(f"  Found in: {', '.join(entry['composites'])}")
            print()
    else:
        print("No unmapped speakers found.")

    print("\nNext steps:")
    print("  1. Resolve unmapped speakers by adding Character() declarations.")
    print("  2. Re-run:  mise run compose-clean")
    print("  3. Then:    mise run apply")
    return 1


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
