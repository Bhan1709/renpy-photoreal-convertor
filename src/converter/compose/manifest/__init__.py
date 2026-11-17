"""Manifest read/write for the compose pass.

Public API
----------

    from converter.compose.manifest import ManifestWriter, KNOWN_FAMILIES
    from converter.compose.manifest import CHARACTER_ALIASES_FILE
"""

from __future__ import annotations

from .writer import (
    CHARACTER_ALIASES_FILE,
    KNOWN_FAMILIES,
    ManifestWriter,
    _load_character_aliases,
    _save_character_aliases,
    _seed_character_aliases,
)

__all__ = [
    "CHARACTER_ALIASES_FILE",
    "KNOWN_FAMILIES",
    "ManifestWriter",
    "_load_character_aliases",
    "_save_character_aliases",
    "_seed_character_aliases",
]
