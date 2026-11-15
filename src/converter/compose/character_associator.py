"""Character association for XXX (scene/adult) composites.

Responsibility: infer which characters are involved in a composite stack in
the ``xxx`` folder, using asset-path stems, Ren'Py ``Character()`` declarations,
and nearby section headings/comments in the declaring script.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from .config import GameConfig
from .errors import ComposeError
from .models import Layer
from .parser import files

_CHARACTER_DEF_RE = re.compile(
    r"""^\s*define\s+([A-Za-z_]\w*)\s*=\s*Character\(\s*(['"])(.*?)\2(.*?)\)\s*$"""
)
_IMAGE_TAG_RE = re.compile(r"""image\s*=\s*(['"])(.*?)\1""")
_HEADING_RE = re.compile(r"^\s*#{3,}\s*\S")
_TOKEN_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+")
_IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")
_WORD_RE = re.compile(r"[A-Za-z]+")


class CharacterAssociator:
    """Infers involved characters from XXX asset paths and Ren'Py definitions."""

    def __init__(self, config: GameConfig) -> None:
        self.config = config
        self._aliases_cache: dict[str, set[str]] | None = None
        self._xxx_tags_cache: set[str] | None = None
        self._display_names: dict[str, str] | None = None

    # -- public API -------------------------------------------------------

    def associate(
        self,
        layers: list[Layer],
        image_name: str,
        block,
        usage_locations: list[dict],
    ) -> list[dict]:
        """Return character associations for a composite in the ``xxx`` folder."""
        aliases = self._aliases()
        xxx_tags = self._xxx_tags()
        evidence: dict[str, set[str]] = defaultdict(set)

        def add_alias(alias: str, source: str, *, allow_short: bool = False) -> None:
            key = _sub_non_alnum(alias).lower()
            if len(key) < 3 and not allow_short:
                return
            for canonical in aliases.get(key, ()):
                if len(key) < 3 and canonical not in xxx_tags:
                    continue
                if canonical in self.config.non_character_folders:
                    continue
                evidence[canonical].add(source)

        def add_identifier_tokens(value: str, source: str) -> None:
            for token in _TOKEN_RE.findall(value):
                if len(token) >= 2:
                    add_alias(token, source, allow_short=True)

        for layer in layers:
            source = (layer.source or "").replace("\\", "/")
            for part in source.split("/")[1:]:
                stem = Path(part).stem
                add_alias(stem, f"asset:{source}", allow_short=True)
                add_identifier_tokens(stem, f"asset:{source}")

        add_alias(image_name, f"image:{image_name}")
        add_identifier_tokens(image_name, f"image:{image_name}")

        if block is not None:
            body = getattr(block, "body", [])
            for _, raw in body:
                text = raw.split("#", 1)[0]
                for token in _IDENTIFIER_RE.findall(text):
                    evidence_source = (
                        f"script_identifier:{block.path.relative_to(self.config.game_root).as_posix()}:{block.line}"
                    )
                    add_alias(token, evidence_source)
                    add_identifier_tokens(token, evidence_source)
            try:
                lines = block.path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
                before = lines[max(0, block.line - 80): block.line - 1]
                base_line = max(1, block.line - 79)
                headings = [
                    (i, raw)
                    for i, raw in enumerate(before, base_line)
                    if _HEADING_RE.match(raw)
                ]
                if headings:
                    index, raw = headings[-1]
                    comment = raw.split("#", 1)[-1].lower()
                    for alias, canonicals in aliases.items():
                        if len(alias) < 3:
                            continue
                        if not re.search(
                            rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", comment
                        ):
                            continue
                        for canonical in canonicals:
                            if canonical not in self.config.non_character_folders:
                                evidence[canonical].add(
                                    f"script_comment:{block.path.relative_to(self.config.game_root).as_posix()}:{index}"
                                )
            except OSError:
                pass

        for location in usage_locations or []:
            statement = location.get("statement", "")
            usage_source = f"script_usage:{location.get('script')}:{location.get('line')}"
            for token in _IDENTIFIER_RE.findall(statement):
                add_identifier_tokens(token, usage_source)
            for alias, canonicals in aliases.items():
                if len(alias) < 3:
                    continue
                if re.search(
                    rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", statement.lower()
                ):
                    for canonical in canonicals:
                        evidence[canonical].add(
                            f"script_usage:{location.get('script')}:{location.get('line')}"
                        )

        return [
            {"character": tag, "evidence": sorted(sources)}
            for tag, sources in sorted(evidence.items())
        ]

    def display_name(self, char_folder: str) -> str:
        """Return the human-readable display name for a character asset folder."""
        if self._display_names is None:
            self._display_names = self._build_display_names()
        return self._display_names.get(char_folder, char_folder)

    # -- internal ---------------------------------------------------------

    def _aliases(self) -> dict[str, set[str]]:
        if self._aliases_cache is None:
            self._aliases_cache, self._xxx_tags_cache = self._build_aliases()
        return self._aliases_cache

    def _xxx_tags(self) -> set[str]:
        if self._xxx_tags_cache is None:
            self._aliases_cache, self._xxx_tags_cache = self._build_aliases()
        return self._xxx_tags_cache

    def _build_aliases(self) -> tuple[dict[str, set[str]], set[str]]:
        chars_dir = self.config.char_dir()
        folders = (
            {p.name.lower() for p in chars_dir.iterdir() if p.is_dir()}
            if chars_dir.is_dir()
            else set()
        )
        aliases: dict[str, set[str]] = defaultdict(set)
        for folder in folders:
            aliases[_sub_non_alnum(folder)].add(folder)

        for script in files(self.config):
            if "archive" in script.parts or "cache" in script.parts:
                continue
            try:
                for raw in script.read_text(encoding="utf-8-sig", errors="replace").splitlines():
                    match = _CHARACTER_DEF_RE.match(raw.split("#", 1)[0].strip())
                    if not match:
                        continue
                    speaker, _, display_name, tail = match.groups()
                    display = _sub_non_alnum(display_name).lower()
                    image_match = _IMAGE_TAG_RE.search(tail)
                    image_tag = image_match.group(2).lower() if image_match else ""
                    candidates: set[str] = set()
                    if display in folders:
                        candidates.add(display)
                    for word in _WORD_RE.findall(display_name.lower()):
                        if word in folders:
                            candidates.add(word)
                    if image_tag in folders:
                        candidates.add(image_tag)
                    if not candidates:
                        canonical = _sub_non_alnum(display_name).lower().strip("_")
                        if canonical:
                            candidates.add(canonical)
                    for canonical in candidates:
                        aliases[display].add(canonical)
                        aliases[_sub_non_alnum(speaker).lower()].add(canonical)
                        if image_tag:
                            aliases[_sub_non_alnum(image_tag)].add(canonical)
            except OSError:
                continue
        return aliases, folders

    def _build_display_names(self) -> dict[str, str]:
        display_by_folder: dict[str, str] = {}
        for script in files(self.config):
            if "archive" in script.parts or "cache" in script.parts:
                continue
            try:
                for raw in script.read_text(encoding="utf-8-sig", errors="replace").splitlines():
                    m = _CHARACTER_DEF_RE.match(raw.split("#", 1)[0].strip())
                    if not m:
                        continue
                    _, _, display_name, tail = m.groups()
                    image_match = _IMAGE_TAG_RE.search(tail)
                    image_tag = image_match.group(2).lower() if image_match else ""
                    chars_dir = self.config.char_dir()
                    folders = (
                        {p.name.lower() for p in chars_dir.iterdir() if p.is_dir()}
                        if chars_dir.is_dir()
                        else set()
                    )
                    candidates: set[str] = set()
                    display_clean = _sub_non_alnum(display_name).lower()
                    if display_clean in folders:
                        candidates.add(display_clean)
                    for word in _WORD_RE.findall(display_name.lower()):
                        if word in folders:
                            candidates.add(word)
                    if image_tag in folders:
                        candidates.add(image_tag)
                    for canonical in candidates:
                        if canonical not in self.config.non_character_folders:
                            if (
                                canonical not in display_by_folder
                                or len(display_name) > len(display_by_folder[canonical])
                            ):
                                display_by_folder[canonical] = display_name
            except OSError:
                continue
        return display_by_folder


def _sub_non_alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _require_config() -> GameConfig:
    raise ComposeError("compose config not initialized; pass a GameConfig explicitly")
