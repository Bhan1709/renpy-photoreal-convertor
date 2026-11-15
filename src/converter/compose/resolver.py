"""Asset path resolution for the compose pass.

Responsibility: map a forward-slash asset path (e.g. ``chars/hazel/boobs/boobs1.png``)
to an absolute file on disk under the configured archive directory, applying
per-game path aliases and case-insensitive folder-suffix matching.
"""

from __future__ import annotations

from pathlib import Path

from .config import GameConfig
from .errors import AssetResolutionError, ComposeError


class Resolver:
    """Resolves asset paths to on-disk files using a :class:`GameConfig`."""

    def __init__(self, config: GameConfig) -> None:
        self.config = config
        # Per-game path alias overrides (e.g. a sprite sheet renamed between versions).
        self._aliases: dict[str, str] = dict(config.path_aliases)

    def with_aliases(self, aliases: dict[str, str]) -> "Resolver":
        """Return a copy with additional path aliases applied."""
        new = Resolver(self.config)
        new._aliases = {**self._aliases, **aliases}
        return new

    def resolve(self, source: str) -> Path | None:
        """Resolve an asset path to an absolute :class:`~pathlib.Path`, or ``None``.

        Resolution strategy:
        1. Apply path aliases from config.
        2. Try direct join under ``archive_dir``.
        3. Fall back to case-insensitive folder-suffix matching under ``archive_dir``.
        """
        s = source
        if s in self._aliases:
            s = self._aliases[s]

        direct = self.config.archive_dir / Path(s)
        if direct.is_file():
            return direct

        # Asset folder variants occur in extracted builds; match exact relative path case-insensitively.
        wanted = s.replace("/", "\\").lower()
        suffix_matches: list[Path] = []
        try:
            candidates = self.config.archive_dir.rglob(Path(s).name)
        except (OSError, ValueError):
            candidates = []

        for candidate in candidates:
            try:
                relative = str(candidate.relative_to(self.config.archive_dir)).replace("/", "\\").lower()
            except ValueError:
                continue
            if relative == wanted:
                return candidate
            if relative.endswith("\\" + wanted):
                suffix_matches.append(candidate)

        if len(suffix_matches) == 1:
            return suffix_matches[0]
        return None

    def archive_relative(self, path: Path) -> str:
        """Return the forward-slash path of ``path`` relative to ``archive_dir``, lowercased."""
        try:
            return path.relative_to(self.config.archive_dir).as_posix().lower()
        except ValueError:
            return ""

    def game_root_relative(self, path: Path) -> str:
        """Return the forward-slash path of ``path`` relative to the game root's parent."""
        try:
            return path.relative_to(self.config.game_root.parent).as_posix()
        except ValueError:
            return path.as_posix()


def _require_config() -> GameConfig:
    raise ComposeError("compose config not initialized; pass a GameConfig explicitly")
