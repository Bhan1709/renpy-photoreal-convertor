"""Ren'Py block parsing for the compose pass.

Responsibility: statically extract structured representations of
``layeredimage`` blocks, ``screen`` blocks, and their layers from
already-read script lines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..config import GameConfig
from ..errors import ComposeError
from ..models import Layer
from ..regexp import (
    BLOCK_RE,
)


class ParseError(ComposeError):
    """Raised when a script cannot be parsed."""


@dataclass(frozen=True)
class ScriptBlock:
    """A parsed ``layeredimage`` or ``screen`` block."""

    kind: str  # "layeredimage" | "screen"
    name: str
    path: Path
    line: int  # 1-based
    body: list[tuple[int, str]]  # list of (line_number, raw_line)


def blocks_from(paths: list[Path]) -> list[ScriptBlock]:
    """Parse all ``layeredimage`` and ``screen`` blocks from the given scripts."""
    out: list[ScriptBlock] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            continue
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


def clean(s: str) -> str:
    """Strip comments and surrounding whitespace from a line."""
    return s.split("#", 1)[0].strip()


def parse_layered(block: ScriptBlock, config: GameConfig) -> list[Layer]:
    """Read source paths and nearby literal attribute/group/condition declarations
    from a ``layeredimage`` block.

    Returns one :class:`Layer` per asset-referencing line, with conditions,
    group, attribute, and offset information populated.
    """
    records: list[Layer] = []
    group = ""
    group_indent: int | None = None
    active_attr: str | None = None
    active_default = False
    attr_indent: int | None = None
    condition_stack: list[tuple[int, str]] = []
    chain_counter = 0
    current_chain: tuple[int, int] | None = None  # (indent, chain_id)

    for line_no, raw in block.body:
        text = clean(raw)
        if not text:
            continue
        indent = len(raw) - len(raw.lstrip())
        condition_stack = [
            (level, cond) for level, cond in condition_stack if level < indent
        ]

        gm = re.match(r"group\s+([\w]+)\s*:", text)
        if group_indent is not None and indent <= group_indent and not gm:
            group = ""
            group_indent = None
            active_attr = None
            active_default = False
            attr_indent = None
        elif attr_indent is not None and indent <= attr_indent:
            active_attr = None
            active_default = False
            attr_indent = None

        if gm:
            group = gm.group(1)
            group_indent = indent
            active_attr = None
            active_default = False
            attr_indent = None
            current_chain = None
            continue

        am = re.match(r"attribute\s+([\w]+)(\s+default)?\s*:", text)
        if am:
            active_attr = am.group(1)
            active_default = bool(am.group(2))
            attr_indent = indent
            current_chain = None
            continue

        cm = re.match(r"(if|elif|else)\s+(.+?)\s*:", text)
        if cm:
            kind, cond = cm.groups()
            if kind == "else":
                cond = "<else>"
            if kind == "if" or current_chain is None or current_chain[0] != indent:
                chain_counter += 1
                current_chain = (indent, chain_counter)
            condition_stack = [
                (level, value) for level, value in condition_stack if level < indent
            ]
            condition_stack.append((indent, cond))
            continue

        if current_chain is not None and indent <= current_chain[0]:
            current_chain = None

        pm = config.path_regex().search(text)
        if pm:
            offset: tuple[float, float] | None = None
            offset_rel: tuple[float, float] | None = None
            xp = re.search(r"\bxpos\s+([\d.]+)\s*", text)
            yp = re.search(r"\bypos\s+([\d.]+)\s*", text)
            if xp and yp:
                offset = (float(xp.group(1)), float(yp.group(1)))
            xa = re.search(r"\bxalign\s+([\d.]+)\s*", text)
            ya = re.search(r"\byalign\s+([\d.]+)\s*", text)
            if xa and ya:
                offset_rel = (float(xa.group(1)), float(ya.group(1)))
            records.append(
                Layer(
                    source=pm.group(0),
                    line=line_no,
                    group=group,
                    conditions=tuple(c for _, c in condition_stack),
                    attribute=active_attr,
                    default_attribute=active_default,
                    offset=offset,
                    offset_rel=offset_rel,
                    chain_id=str(current_chain[1]) if current_chain else None,
                )
            )
    return records


def parse_screen_stack(
    block: ScriptBlock,
    definitions: dict[str, tuple[ScriptBlock, list[Layer]]],
    config: GameConfig,
) -> list[Layer]:
    """Parse the body of a ``screen`` block into a stack of layers.

    Resolves ``add <path>`` and named layeredimage displayables, tracking
    if/elif/else chains so mutually-exclusive alternatives are kept together.
    """
    stack: list[Layer] = []
    conditions: list[tuple[int, str]] = []
    chain_counter = 0
    current_chain: tuple[int, int] | None = None

    for line_no, row in block.body:
        text = clean(row)
        if not text:
            continue
        indent = len(row) - len(row.lstrip())
        conditions = [(level, cond) for level, cond in conditions if level < indent]

        cm = re.match(r"(if|elif|else)\s+(.+?)\s*:", text)
        if cm:
            kind, cond = cm.groups()
            if kind == "else":
                cond = "<else>"
            # An ``if`` always starts a new chain (even at the same indent as a
            # previous ``if``/``elif`` block); ``elif``/``else`` continue the
            # current chain when at the same indent.  This mirrors
            # :func:`parse_layered` and keeps independent ``if`` blocks from
            # becoming mutually exclusive (Optimization fix).
            if kind == "if" or current_chain is None or current_chain[0] != indent:
                chain_counter += 1
                current_chain = (indent, chain_counter)
            conditions = [(level, cond) for level, cond in conditions if level < indent]
            conditions.append((indent, cond))
            continue

        if text == "else:":
            if current_chain is None or current_chain[0] != indent:
                chain_counter += 1
                current_chain = (indent, chain_counter)
            conditions = [(level, cond) for level, cond in conditions if level < indent]
            conditions.append((indent, "<else>"))
            continue

        if current_chain is not None and indent <= current_chain[0]:
            current_chain = None

        chain_id = f"screen:{current_chain[1]}" if current_chain else None

        quoted = re.match(r'add\s+([\'"])(.+?)\1', text)
        if quoted and config.path_regex().search(quoted.group(2)):
            offset, offset_rel = _parse_position_from_transforms(
                text[quoted.end() :].strip()
            )
            stack.append(
                Layer(
                    source=quoted.group(2),
                    line=line_no,
                    conditions=tuple(c for _, c in conditions),
                    role="screen_add",
                    offset=offset,
                    offset_rel=offset_rel,
                    chain_id=chain_id,
                )
            )
        elif quoted and quoted.group(2) in definitions:
            tag = quoted.group(2)
            _extend_with_definition(
                stack, tag, definitions[tag][1], conditions, chain_id
            )
        elif quoted and quoted.group(2).lower().startswith(
            tuple(f"{n}/" for n in config.categories.values())
        ):
            offset, offset_rel = _parse_position_from_transforms(
                text[quoted.end() :].strip()
            )
            stack.append(
                Layer(
                    source=quoted.group(2),
                    line=line_no,
                    conditions=tuple(c for _, c in conditions),
                    role="screen_add",
                    offset=offset,
                    offset_rel=offset_rel,
                    chain_id=chain_id,
                )
            )
        else:
            named = re.match(r"add\s+([\w.]+)", text)
            if named and named.group(1) in definitions:
                tag = named.group(1)
                _extend_with_definition(
                    stack, tag, definitions[tag][1], conditions, chain_id
                )
    return stack


def _extend_with_definition(
    stack: list[Layer],
    tag: str,
    definition_layers: list[Layer],
    conditions: list[tuple[int, str]],
    chain_id: str | None,
) -> None:
    """Append layers expanded from a named layeredimage definition."""
    for layer in definition_layers:
        new_chain_id = None
        if layer.chain_id is not None:
            new_chain_id = f"def:{tag}:{layer.chain_id}"
        elif chain_id is not None:
            new_chain_id = chain_id
        stack.append(
            layer.replace(
                conditions=tuple(c for _, c in conditions) + tuple(layer.conditions),
                via_image_tag=tag,
                chain_id=new_chain_id,
            )
        )


# These are used by both blocks.py and declarations.py but live here to avoid
# a circular import between blocks and declarations.
def _parse_position_from_transforms(
    transforms: str,
) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    """Extract xpos/ypos or xalign/yalign from transform text."""
    offset: tuple[float, float] | None = None
    offset_rel: tuple[float, float] | None = None
    xp = re.search(r"\bxpos\s+([\d.]+)\s*", transforms)
    yp = re.search(r"\bypos\s+([\d.]+)\s*", transforms)
    if xp and yp:
        offset = (float(xp.group(1)), float(yp.group(1)))
    xa = re.search(r"\bxalign\s+([\d.]+)\s*", transforms)
    ya = re.search(r"\byalign\s+([\d.]+)\s*", transforms)
    if xa and ya:
        offset_rel = (float(xa.group(1)), float(ya.group(1)))
    return offset, offset_rel
