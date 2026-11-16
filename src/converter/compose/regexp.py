"""Compiled regular expressions shared across the compose subpackage.

All regex patterns live here so they are defined once and imported wherever
needed, instead of being duplicated in multiple modules.
"""

from __future__ import annotations

import re

# Matches:  <indent><layeredimage|screen> <name>(...):  at block start
BLOCK_RE = re.compile(
    r"^(\s*)(layeredimage|screen)\s+([\w.]+)(?:\([^\n]*\))?\s*:\s*$"
)

# Matches:  <indent>image <name> = <expression>
IMAGE_RE = re.compile(r"^(\s*)image\s+([\w.]+)\s*=\s*(.*)$")

# Matches:  show|scene <tag> [<transform args>]
SHOW_SCENE_RE = re.compile(r"^\s*(?:show|scene)\s+(?!screen\b)([\w.]+)(?:\s+([^\n#]+))?")

# Matches:  show screen <name>
SHOW_SCREEN_RE = re.compile(r"^\s*show\s+screen\s+([\w.]+)")

# Matches:  show|scene <tag> (simple, used by the apply pass)
SHOW_RE = re.compile(r"^(show|scene)\s+\S+")

# Matches:  default <name> = <value>
DEFAULT_RE = re.compile(
    r"^\s*default\s+([A-Za-z_]\w*)\s*=\s*(True|False|-?\d+(?:\.\d+)?|'[^']*'|\"[^\"]*\")"
)

# Matches:  define <var> = Character("<display>", ... image="<tag>" ...)
CHARACTER_DEF_RE = re.compile(
    r"""^\s*define\s+([A-Za-z_]\w*)\s*=\s*Character\(\s*(['"])(.*?)\2(.*?)\)\s*$"""
)

# Matches:  image <tag> = "<path>"  (inside Character() definition tail)
IMAGE_TAG_RE = re.compile(r"""image\s*=\s*(['"])(.*?)\1""")

# Matches:  show|scene <tag> [<transform args>]  (used by composer for declarations)
DECLARATION_USE_RE = re.compile(r"^\s*(?:show|scene)\s+([\w.]+)")

# Keywords that are not dialogue speakers even when followed by a quoted string.
NOT_SPEAKER: frozenset[str] = frozenset({
    "add", "show", "scene", "hide", "pause", "jump", "call", "return", "with",
    "as", "to", "parent", "zorder", "if", "elif", "else", "while", "for",
    "in", "not", "and", "or", "True", "False", "None", "screen", "layeredimage",
    "image", "define", "default", "init", "label", "menu", "python",
})

# Matches:  <speaker> "<dialogue>"
SAY_RE = re.compile(r'^(\s*)([A-Za-z_]\w*)\s+(".*")(\s*#.*)?$')

# Matches:  "<narrator dialogue>"
NARRATOR_RE = re.compile(r'^(\s*)(".*")(\s*#.*)?$')

# Character associator patterns
HEADING_RE = re.compile(r"^\s*#{3,}\s*\S")
TOKEN_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+")
IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")
WORD_RE = re.compile(r"[A-Za-z]+")

__all__ = [
    "BLOCK_RE",
    "CHARACTER_DEF_RE",
    "DECLARATION_USE_RE",
    "DEFAULT_RE",
    "HEADING_RE",
    "IDENTIFIER_RE",
    "IMAGE_RE",
    "IMAGE_TAG_RE",
    "NARRATOR_RE",
    "NOT_SPEAKER",
    "SAY_RE",
    "SHOW_RE",
    "SHOW_SCENE_RE",
    "SHOW_SCREEN_RE",
    "TOKEN_RE",
    "WORD_RE",
]
