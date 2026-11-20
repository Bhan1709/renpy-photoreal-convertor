"""Constants shared across the decompile subpackage."""

from __future__ import annotations

from pathlib import Path

ROOT = Path("src")
RENPY_GAMES = ROOT / "renpy_game"
GAME_FOLDER = RENPY_GAMES / "game"
CONFIG_PATH = ROOT / "converter" / "config.json"

# File kinds we consolidate when staging a game.
COPY_EXTENSIONS = {".rpa", ".rpyc", ".rpymc", ".rpy"}
COPY_FILENAMES = {"_ren.py"}

# Image file extensions we care about for category classification.
IMAGE_EXTENSIONS = {".png", ".webp", ".avif", ".jpeg", ".jpg"}

# Ren'Py folder name patterns per image category (matched case-insensitively).
# "images" is deliberately omitted: it is ambiguous (used for both char and
# scene) so it is safer to fall back to mode-based classification when only
# "images" exists.
CATEGORY_FOLDER_PATTERNS: dict[str, set[str]] = {
    "bg": {"bgs", "bg", "background", "backgrounds"},
    "gui": {"gui", "ui", "interface"},
    "char": {"chars", "char", "character", "characters"},
    "scene": {"scene", "scenes", "xxx"},
}
