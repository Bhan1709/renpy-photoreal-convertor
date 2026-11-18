"""Character association for XXX (scene/adult) composites.

Responsibility: infer which characters are involved in a composite stack in
the ``xxx`` folder, using asset-path stems, Ren'Py ``Character()`` declarations,
and nearby section headings/comments in the declaring script.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ..config import GameConfig
from ..errors import ComposeError
from ..models import Layer
from ..parsing.files import files
from ..regexp import (
    CHARACTER_DEF_RE,
    HEADING_RE,
    IDENTIFIER_RE,
    IMAGE_TAG_RE,
    TOKEN_RE,
    WORD_RE,
)


@dataclass(frozen=True)
class CharacterDeclaration:
    """Canonical character data derived from Ren'Py ``Character(...)`` calls."""

    name: str
    aliases: frozenset[str]
    image_tags: frozenset[str]


class CharacterAssociator:
    """Infers involved characters from XXX asset paths and Ren'Py definitions."""

    def __init__(
        self,
        config: GameConfig,
        script_lines: dict[Path, tuple[str, ...]] | None = None,
    ) -> None:
        self.config = config
        # Indexed per-file source lines (path -> lines) so the associator does
        # not re-read scripts from disk.  Falls back to reading per-file when
        # not supplied.
        self._script_lines: dict[Path, tuple[str, ...]] = script_lines or {}
        self._aliases_cache: dict[str, set[str]] | None = None
        self._xxx_tags_cache: set[str] | None = None
        self._display_names: dict[str, str] | None = None
        self._declarations_cache: dict[str, CharacterDeclaration] | None = None

    # -- public API -------------------------------------------------------

    def associate(
        self,
        layers: list[Layer],
        image_name: str,
        block,
        usage_locations: list[dict],
        lines: tuple[str, ...] | list[str] | None = None,
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
            for token in TOKEN_RE.findall(value):
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
                for token in IDENTIFIER_RE.findall(text):
                    evidence_source = f"script_identifier:{block.path.relative_to(self.config.game_root).as_posix()}:{block.line}"
                    add_alias(token, evidence_source)
                    add_identifier_tokens(token, evidence_source)
            try:
                # Use indexed lines when available; fall back to reading the
                # definition script from disk (Optimization #1).
                if lines is None:
                    try:
                        lines = block.path.read_text(
                            encoding="utf-8-sig", errors="replace"
                        ).splitlines()
                    except OSError:
                        lines = []
                assert lines is not None
                before = lines[max(0, block.line - 80) : block.line - 1]
                base_line = max(1, block.line - 79)
                headings = [
                    (i, raw)
                    for i, raw in enumerate(before, base_line)
                    if HEADING_RE.match(raw)
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
            usage_source = (
                f"script_usage:{location.get('script')}:{location.get('line')}"
            )
            for token in IDENTIFIER_RE.findall(statement):
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

    def known_char_folders(self) -> frozenset[str]:
        """Return the set of known character folder names under ``chars/``."""
        self._aliases()  # ensure caches are populated
        return frozenset(self._xxx_tags_cache or set())

    def resolve_speaker(self, speaker: str) -> set[str]:
        """Resolve a speaker name to canonical char folder(s) via the alias map.

        The alias map is built from Ren'Py ``Character()`` declarations: e.g.
        ``define h = Character("Hazel", ...)`` maps speaker ``"h"`` to char
        folder ``"hazel"``.  Returns the set of matching canonical folder
        names (empty if the speaker cannot be resolved).
        """
        self._aliases()  # ensure caches are populated
        key = _sub_non_alnum(speaker)
        if not key:
            return set()
        return set(self._aliases().get(key, set()))

    def display_name(self, char_folder: str) -> str:
        """Return the human-readable display name for a character asset folder."""
        if self._display_names is None:
            (
                self._aliases_cache,
                self._xxx_tags_cache,
                self._display_names,
                self._declarations_cache,
            ) = self._build_aliases_and_display_names()
        return self._display_names.get(char_folder, char_folder)

    def character_declarations(self) -> dict[str, CharacterDeclaration]:
        """Return characters discovered only from ``Character(...)`` declarations.

        The alias map used for speaker resolution also contains self-aliases for
        every physical source directory. Those directories are intentionally
        excluded here so callers can build a declaration-only character
        catalog.
        """
        if self._declarations_cache is None:
            (
                self._aliases_cache,
                self._xxx_tags_cache,
                self._display_names,
                self._declarations_cache,
            ) = self._build_aliases_and_display_names()
        return dict(self._declarations_cache)

    # -- internal ---------------------------------------------------------

    def _aliases(self) -> dict[str, set[str]]:
        if self._aliases_cache is None:
            (
                self._aliases_cache,
                self._xxx_tags_cache,
                self._display_names,
                self._declarations_cache,
            ) = self._build_aliases_and_display_names()
        return self._aliases_cache

    def _xxx_tags(self) -> set[str]:
        if self._xxx_tags_cache is None:
            (
                self._aliases_cache,
                self._xxx_tags_cache,
                self._display_names,
                self._declarations_cache,
            ) = self._build_aliases_and_display_names()
        return self._xxx_tags_cache

    def _build_aliases_and_display_names(
        self,
    ) -> tuple[
        dict[str, set[str]],
        set[str],
        dict[str, str],
        dict[str, CharacterDeclaration],
    ]:
        """Build the alias map and display-name map in a single script walk.

        ``_build_aliases`` and ``_build_display_names`` used to walk every
        ``.rpy`` file separately, regex-matching every line each time.
        Optimization #4 merges them into one pass: each file is opened and read
        exactly once, and both structures are populated from the same matches.
        """
        chars_dir = self.config.char_dir()
        folders = (
            {p.name.lower() for p in chars_dir.iterdir() if p.is_dir()}
            if chars_dir.is_dir()
            else set()
        )
        aliases: dict[str, set[str]] = defaultdict(set)
        declared_aliases: dict[str, set[str]] = defaultdict(set)
        declared_image_tags: dict[str, set[str]] = defaultdict(set)
        display_by_folder: dict[str, str] = {}
        for folder in folders:
            aliases[_sub_non_alnum(folder)].add(folder)

        # Use indexed per-file lines when available so each script is read only
        # once by the single-pass indexer rather than scanned again here
        # (Optimization #1).  Fall back to reading each file directly.
        script_lines = self._script_lines
        if script_lines:
            script_items: list[tuple[Path, tuple[str, ...]]] = list(
                script_lines.items()
            )
        else:
            include = set(self.config.rpy_files) if self.config.rpy_files else None
            script_items = [
                (script, ()) for script in files(self.config, include=include)
            ]
        for script, file_lines in script_items:
            lines: tuple[str, ...] | list[str] = file_lines
            if not lines:
                try:
                    lines = script.read_text(
                        encoding="utf-8-sig", errors="replace"
                    ).splitlines()
                except OSError:
                    continue
            for raw in lines:
                match = CHARACTER_DEF_RE.match(raw.strip())
                if not match:
                    continue
                speaker, _, display_name, tail = match.groups()
                display = _sub_non_alnum(display_name).lower()
                speaker_key = _sub_non_alnum(speaker).lower()
                image_match = IMAGE_TAG_RE.search(tail)
                image_tag = image_match.group(2).lower() if image_match else ""
                image_key = _sub_non_alnum(image_tag)
                candidates: set[str] = set()
                if display in folders:
                    candidates.add(display)
                for word in WORD_RE.findall(display_name.lower()):
                    if word in folders:
                        candidates.add(word)
                if image_tag in folders:
                    candidates.add(image_tag)
                if not candidates:
                    canonical = _sub_non_alnum(display_name).lower().strip("_")
                    if canonical:
                        candidates.add(canonical)
                # Alias map: speaker/display/image-tag nicknames -> canonical folder.
                for canonical in candidates:
                    aliases[display].add(canonical)
                    aliases[speaker_key].add(canonical)
                    if image_key:
                        aliases[image_key].add(canonical)
                    declared_aliases[canonical].update(
                        alias for alias in (display, speaker_key, image_key) if alias
                    )
                    if image_key:
                        declared_image_tags[canonical].add(image_key)
                # Display-name map: canonical folder -> longest display name.
                for canonical in candidates:
                    if canonical in self.config.non_character_folders:
                        continue
                    if canonical not in display_by_folder or len(display_name) > len(
                        display_by_folder[canonical]
                    ):
                        display_by_folder[canonical] = display_name
        declarations = {
            name: CharacterDeclaration(
                name=name,
                aliases=frozenset(declared_aliases[name]),
                image_tags=frozenset(declared_image_tags[name]),
            )
            for name in sorted(declared_aliases)
        }
        return aliases, folders, display_by_folder, declarations


def _sub_non_alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _require_config() -> GameConfig:
    raise ComposeError("compose config not initialized; pass a GameConfig explicitly")
