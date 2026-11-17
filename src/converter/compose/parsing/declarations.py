"""Image declaration parsing for the compose pass.

Responsibility: index all ``image <name> = <expression>`` declarations that
reference asset folders, from already-read script lines.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import GameConfig
from ..regexp import IMAGE_RE


@dataclass(frozen=True)
class ImageDeclaration:
    """A parsed ``image <name> = <expression>`` declaration."""

    name: str
    script: Path
    line: int  # 1-based
    expression: str
    sources: tuple[str, ...]


def parse_image_declarations(
    script_files: list[Path],
    config: GameConfig,
) -> dict[str, ImageDeclaration]:
    """Index all ``image`` declarations that reference asset paths."""
    declarations: dict[str, ImageDeclaration] = {}
    for path in script_files:
        source_lines = path.read_text(
            encoding="utf-8-sig", errors="replace"
        ).splitlines()
        i = 0
        while i < len(source_lines):
            raw = source_lines[i]
            im = IMAGE_RE.match(clean(raw))
            if im:
                _, name, expression = im.groups()
                declaration_indent = len(raw) - len(raw.lstrip())
                expr_lines = [expression]
                j = i + 1
                while j < len(source_lines):
                    following = source_lines[j]
                    if (
                        following.strip()
                        and len(following) - len(following.lstrip())
                        <= declaration_indent
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


def clean(s: str) -> str:
    """Strip comments and surrounding whitespace from a line."""
    return s.split("#", 1)[0].strip()
