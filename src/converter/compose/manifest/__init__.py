"""Manifest read/write for the compose pass.

Public API
----------

    from converter.compose.manifest import ManifestWriter, KNOWN_FAMILIES
    from converter.compose.manifest import CHARACTERS_FILE
"""

from __future__ import annotations

from .writer import (
    CHARACTERS_FILE,
    KNOWN_FAMILIES,
    ManifestWriter,
    _load_characters,
    _save_characters,
    _seed_characters,
)

__all__ = [
    "CHARACTERS_FILE",
    "KNOWN_FAMILIES",
    "ManifestWriter",
    "_load_characters",
    "_save_characters",
    "_seed_characters",
]
