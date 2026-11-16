"""Asset path resolution for the compose pass.

Responsibility: map a forward-slash asset path (e.g. ``chars/hazel/boobs/boobs1.png``)
to an absolute file on disk under the configured archive directory, applying
per-game path aliases and case-insensitive folder-suffix matching.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from .config import GameConfig
from .errors import ComposeError


class Resolver:
    """Resolves asset paths to on-disk files using a :class:`GameConfig`.

    The archive directory is indexed once in :meth:`__init__` (Optimization #3),
    so :meth:`resolve` does dictionary lookups instead of walking the archive
    tree with :meth:`Path.rglob` for every unresolved asset.
    """

    def __init__(self, config: GameConfig) -> None:
        self.config = config
        # Per-game path alias overrides (e.g. a sprite sheet renamed between versions).
        self._aliases: dict[str, str] = dict(config.path_aliases)
        # Indexes built once from the archive directory:
        #   _archive_index: lowercased relative posix path -> absolute Path
        #   _name_index:    lowercased file name -> list of absolute Paths
        self._archive_index: dict[str, Path] = {}
        self._name_index: dict[str, list[Path]] = defaultdict(list)
        # Cache of source -> resolved Path (or None).  Avoids repeating
        # stat() calls and index lookups for the same asset across many
        # composites (Optimization #6).
        self._resolve_cache: dict[str, Path | None] = {}
        self._build_archive_index()

    def with_aliases(self, aliases: dict[str, str]) -> Resolver:
        """Return a copy with additional path aliases applied."""
        new = Resolver(self.config)
        new._aliases = {**self._aliases, **aliases}
        return new

    def _build_archive_index(self) -> None:
        """Walk ``archive_dir`` once and index every file it contains."""
        archive = self.config.archive_dir
        if not archive.is_dir():
            return
        try:
            for path in archive.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    relative = path.relative_to(archive).as_posix().lower()
                except ValueError:
                    continue
                # Two files cannot share one relative path; first occurrence wins.
                self._archive_index.setdefault(relative, path)
                self._name_index[path.name.lower()].append(path)
        except (OSError, ValueError):
            # An unreadable archive directory falls back to direct-join only.
            pass

    def resolve(self, source: str) -> Path | None:
        """Resolve an asset path to an absolute :class:`~pathlib.Path`, or ``None``.

        Resolution strategy:
        1. Apply path aliases from config.
        2. Try direct join under ``archive_dir``.
        3. Look up the exact relative path case-insensitively in the archive index.
        4. Fall back to case-insensitive folder-suffix matching using the name index
           (no directory walk).
        """
        s = source
        if s in self._aliases:
            s = self._aliases[s]

        # Cache hit — skip stat() and index lookups entirely.
        if s in self._resolve_cache:
            return self._resolve_cache[s]

        direct = self.config.archive_dir / Path(s)
        if direct.is_file():
            self._resolve_cache[s] = direct
            return direct

        # Asset folder variants occur in extracted builds; match exact relative path
        # case-insensitively via the index (no rglob walk).
        key = s.replace("\\", "/").lower()
        exact = self._archive_index.get(key)
        if exact is not None:
            self._resolve_cache[s] = exact
            return exact

        wanted = key.replace("/", "\\")
        suffix_matches: list[Path] = []
        for candidate in self._name_index.get(Path(s).name.lower(), []):
            try:
                relative = str(candidate.relative_to(self.config.archive_dir)).replace("/", "\\").lower()
            except ValueError:
                continue
            if relative == wanted:
                self._resolve_cache[s] = candidate
                return candidate
            if relative.endswith("\\" + wanted):
                suffix_matches.append(candidate)

        if len(suffix_matches) == 1:
            self._resolve_cache[s] = suffix_matches[0]
            return suffix_matches[0]
        self._resolve_cache[s] = None
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
