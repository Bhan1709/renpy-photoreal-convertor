"""Ren'Py script parsing for the compose pass.

Responsibility: statically scan ``.rpy`` scripts and extract structured
representations of ``layeredimage`` blocks, ``screen`` blocks, and ``image``
declarations that reference asset folders.

This package does *not* composite images or write files — it only parses.
All game-specific folder names come from :class:`~converter.compose.config.GameConfig`.

Public API
----------

    from converter.compose.parsing import (
        files, index_scripts, parse_screen_stack, parse_layered,
        ScriptBlock, ScriptIndex, FileIndex, ScriptUsage,
        ImageDeclaration, find_nearby_dialog, DialogueLine,
        SHOW_SCENE_RE, SHOW_SCREEN_RE,
    )
"""

from __future__ import annotations

# Shared compiled regexes (canonical home is converter.compose.regexp).
from ..regexp import SHOW_SCENE_RE, SHOW_SCREEN_RE
from .blocks import (
    BLOCK_RE as _BLOCK_RE,  # noqa: F401  (re-exported via regexp)
)
from .blocks import (
    ScriptBlock,
    blocks_from,
    clean,
    parse_layered,
    parse_screen_stack,
)
from .declarations import ImageDeclaration, parse_image_declarations
from .dialog import DialogueLine, find_nearby_dialog
from .files import files
from .indexer import (
    FileIndex,
    ScriptIndex,
    ScriptUsage,
    index_scripts,
)

__all__ = [
    "SHOW_SCENE_RE",
    "SHOW_SCREEN_RE",
    "DialogueLine",
    "FileIndex",
    "ImageDeclaration",
    "ScriptBlock",
    "ScriptIndex",
    "ScriptUsage",
    "blocks_from",
    "clean",
    "files",
    "find_nearby_dialog",
    "index_scripts",
    "parse_image_declarations",
    "parse_layered",
    "parse_screen_stack",
]
