"""Single-pass script indexing for the compose pass.

Responsibility: read every ``.rpy`` file once and build a complete in-memory
index of blocks, declarations, defaults, usages, and character definitions.
Replaces the 6–8 redundant full-file reads that the old pipeline performed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..config import GameConfig
from ..models import Layer
from ..regexp import (
    CHARACTER_DEF_RE,
    IMAGE_TAG_RE,
    SHOW_SCENE_RE,
    SHOW_SCREEN_RE,
)
from .blocks import ScriptBlock, parse_layered
from .declarations import ImageDeclaration
from .files import files


@dataclass(frozen=True)
class ScriptUsage:
    """A usage of a tag/declaration/screen found in a script during indexing."""

    script: Path
    line: int
    statement: str
    tag: str
    kind: str  # "show_scene" or "show_screen"


@dataclass(frozen=True)
class FileIndex:
    """Everything extracted from a single .rpy file in one read."""

    path: Path
    lines: tuple[str, ...]  # full file text, kept so downstream passes never re-read
    blocks: list[ScriptBlock]
    image_declarations: dict[str, ImageDeclaration]
    defaults: dict[str, object]
    usages: list[ScriptUsage]
    character_defs: list[tuple[str, str, str]]  # (speaker, display_name, image_tag)


@dataclass(frozen=True)
class ScriptIndex:
    """Single-pass index of all .rpy scripts — each file read exactly once."""

    files: list[Path]
    per_file: dict[Path, FileIndex]
    layeredimage_defs: dict[str, tuple[ScriptBlock, list[Layer]]]
    screens: dict[str, ScriptBlock]
    image_decls: dict[str, ImageDeclaration]
    all_defaults: dict[str, object]


def index_scripts(config: GameConfig) -> ScriptIndex:
    """Read every .rpy file once and build a complete in-memory index.

    Replaces the 6–8 redundant full-file reads that the old pipeline performed
    (blocks, declarations, defaults, three usage scans, two character-associator
    walks).  Each file is opened and read exactly once here; every downstream
    pass consumes the in-memory index instead of re-reading from disk.
    """
    include = set(config.rpy_files) if config.rpy_files else None
    script_files = files(config, include=include)
    per_file: dict[Path, FileIndex] = {}
    layeredimage_defs: dict[str, tuple[ScriptBlock, list[Layer]]] = {}
    screens: dict[str, ScriptBlock] = {}
    image_decls: dict[str, ImageDeclaration] = {}
    all_defaults: dict[str, object] = dict(config.game_defaults)

    for path in script_files:
        try:
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            continue

        blocks = _parse_blocks_in_lines(lines, path)
        decls = _parse_declarations_in_lines(lines, path, config)
        defaults = _parse_defaults_in_lines(lines)
        usages = _find_all_usages_in_lines(lines, path)
        char_defs = _parse_character_defs_in_lines(lines, path)

        per_file[path] = FileIndex(
            path=path,
            lines=tuple(lines),
            blocks=blocks,
            image_declarations=decls,
            defaults=defaults,
            usages=usages,
            character_defs=char_defs,
        )

        for block in blocks:
            if block.kind == "layeredimage" and block.name not in layeredimage_defs:
                layeredimage_defs[block.name] = (block, parse_layered(block, config))
            elif block.kind == "screen" and block.name not in screens:
                screens[block.name] = block

        for name, decl in decls.items():
            if name not in image_decls:
                image_decls[name] = decl

        for key, value in defaults.items():
            if key not in all_defaults:
                all_defaults[key] = value

    return ScriptIndex(
        files=script_files,
        per_file=per_file,
        layeredimage_defs=layeredimage_defs,
        screens=screens,
        image_decls=image_decls,
        all_defaults=all_defaults,
    )


# --- Single-file parsing helpers (operate on already-read lines) ---


def _parse_blocks_in_lines(lines: list[str], path: Path) -> list[ScriptBlock]:
    """Parse layeredimage/screen blocks from already-read lines."""
    out: list[ScriptBlock] = []
    i = 0
    while i < len(lines):
        m = BLOCK_RE.match(lines[i])
        if not m:
            i += 1
            continue
        indent = len(m.group(1))
        kind = m.group(2)
        name = m.group(3)
        start = i
        i += 1
        body: list[tuple[int, str]] = []
        while i < len(lines):
            raw = lines[i]
            if raw.strip() and len(raw) - len(raw.lstrip()) <= indent:
                break
            body.append((i + 1, raw))
            i += 1
        out.append(
            ScriptBlock(kind=kind, name=name, path=path, line=start + 1, body=body)
        )
    return out


def _parse_declarations_in_lines(
    lines: list[str], path: Path, config: GameConfig
) -> dict[str, ImageDeclaration]:
    """Parse image declarations from already-read lines."""
    declarations: dict[str, ImageDeclaration] = {}
    i = 0
    while i < len(lines):
        raw = lines[i]
        im = IMAGE_RE.match(clean(raw))
        if im:
            _, name, expression = im.groups()
            declaration_indent = len(raw) - len(raw.lstrip())
            expr_lines = [expression]
            j = i + 1
            while j < len(lines):
                following = lines[j]
                if (
                    following.strip()
                    and len(following) - len(following.lstrip()) <= declaration_indent
                ):
                    break
                expr_lines.append(clean(following))
                j += 1
            expression = "\n".join(expr_lines)
            sources = tuple(
                m.group(0) for m in config.path_regex().finditer(expression)
            )
            if sources and name not in declarations:
                declarations[name] = ImageDeclaration(
                    name=name,
                    script=path,
                    line=i + 1,
                    expression=expression,
                    sources=sources,
                )
            i = j
        else:
            i += 1
    return declarations


def _parse_literal(value: str) -> bool | int | float | str | None:
    """Parse a Ren'Py default literal into a Python value, or None."""
    value = value.strip()
    if value == "True":
        return True
    if value == "False":
        return False
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if value[:1] in ("'", '"') and value[-1:] == value[:1]:
        return value[1:-1]
    return None


def _parse_defaults_in_lines(lines: list[str]) -> dict[str, object]:
    """Parse default variable assignments from already-read lines."""
    defaults: dict[str, object] = {}
    for raw in lines:
        dm = re.match(
            r"^\s*default\s+([A-Za-z_]\w*)\s*=\s*(True|False|-?\d+(?:\.\d+)?|'[^']*'|\"[^\"]*\")",
            raw.split("#", 1)[0].strip(),
        )
        if dm:
            key, value = dm.groups()
            parsed = _parse_literal(value)
            if parsed is not None:
                defaults[key] = parsed
    return defaults


def _find_all_usages_in_lines(lines: list[str], path: Path) -> list[ScriptUsage]:
    """Find all show/scene/show-screen usage lines in already-read lines."""
    usages: list[ScriptUsage] = []
    for no, raw in enumerate(lines, 1):
        text = clean(raw)
        m = SHOW_SCENE_RE.match(text)
        if m:
            usages.append(
                ScriptUsage(
                    script=path,
                    line=no,
                    statement=text,
                    tag=m.group(1),
                    kind="show_scene",
                )
            )
            continue
        sm = SHOW_SCREEN_RE.match(text)
        if sm:
            usages.append(
                ScriptUsage(
                    script=path,
                    line=no,
                    statement=text,
                    tag=sm.group(1),
                    kind="show_screen",
                )
            )
    return usages


def _parse_character_defs_in_lines(
    lines: list[str], path: Path
) -> list[tuple[str, str, str]]:
    """Extract (speaker, display_name, image_tag) from Character() definitions in lines."""
    defs: list[tuple[str, str, str]] = []
    for raw in lines:
        match = CHARACTER_DEF_RE.match(raw.split("#", 1)[0].strip())
        if not match:
            continue
        speaker, _, display_name, tail = match.groups()
        image_match = IMAGE_TAG_RE.search(tail)
        image_tag = image_match.group(2).lower() if image_match else ""
        defs.append((speaker, display_name, image_tag))
    return defs


# Regexes compiled locally (the shared set lives in ..regexp, but these are
# only used by the indexer internals).
BLOCK_RE = re.compile(r"^(\s*)(layeredimage|screen)\s+([\w.]+)(?:\([^\n]*\))?\s*:\s*$")
IMAGE_RE = re.compile(r"^(\s*)image\s+([\w.]+)\s*=\s*(.*)$")


def clean(s: str) -> str:
    """Strip comments and surrounding whitespace from a line."""
    return s.split("#", 1)[0].strip()
