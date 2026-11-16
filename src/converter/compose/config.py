"""Game configuration for the compose subpackage.

Defines :class:`GameConfig`, which describes how a single Ren'Py game maps its
four canonical component categories (bg, char, scene, gui) onto on-disk asset
folders, plus a single loader that reads the decompile-generated
``src/converter/config.json``.

All paths in ``config.json`` are relative to the project root (the directory
containing ``src/``).  ``game_folder`` is resolved against the project root,
and ``archive_dir`` is always ``game_folder / archive``.  Output locations
default to ``<game_folder>/composites/...`` via :meth:`resolved_paths`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

# Project root: this file lives at src/converter/compose/config.py, so
# parents[3] is the repository root where config.json and src/ live.
PROJECT_ROOT: Path = Path(__file__).resolve().parents[3]

# Canonical component categories shared by every Ren'Py game.
CANONICAL_CATEGORIES: frozenset[str] = frozenset({"bg", "char", "scene", "gui"})

# Category keys used when referring to Ren'Py asset folders on disk.  These are
# the *folder names* under the extracted archive directory, not the canonical
# categories above.  The mapping category -> folder is game-specific.
DEFAULT_CATEGORY_FOLDERS: dict[str, str] = {
    "bg": "bgs",
    "char": "chars",
    "scene": "xxx",
    "gui": "gui",
}

# Folders under the character asset directory that are not themselves
# characters (e.g. effects, props).  Game-specific; overridable via config.
DEFAULT_NON_CHARACTER_FOLDERS: frozenset[str] = frozenset(
    {"fx", "props", "halloween", "war", "dnd", "photos"}
)

# Path alias overrides applied when resolving asset sources.  Keys and values
# are forward-slash separated paths relative to the archive directory.
DEFAULT_PATH_ALIASES: dict[str, str] = {
    "chars/hazel/boobs/boobs1.png": "chars/hazel/boobs/boobs2.png",
    "chars/hazel/boobs/nips1.png": "chars/hazel/boobs/nips2.png",
    "chars/hazel/big/face/eyesClosed.png": "chars/hazel/face/eyesClosed.png",
}

# Default variable values assumed when no `default` statement is found.
DEFAULT_GAME_DEFAULTS: dict[str, object] = {
    "month": 6,
    "day": 1,
}


@dataclass(frozen=True)
class GameConfig:
    """Describes one Ren'Py game's asset layout and compose output locations.

    Attributes:
        name: Human-readable game identifier (used for reports/output naming).
        game_root: Absolute path to the Ren'Py ``game`` folder (containing the
            ``.rpy`` scripts and, typically, an ``archive/`` subdirectory).
        archive_dir: Absolute path to the extracted asset archive.  Always
            ``game_root / "archive"``.
        categories: Mapping of canonical category -> asset folder name under
            ``archive_dir``.  Must cover all of :data:`CANONICAL_CATEGORIES`.
        non_character_folders: Subfolder names under the character directory
            that are not treated as characters.
        path_aliases: Forward-slash asset paths (relative to ``archive_dir``)
            that should be redirected to an alternate path when resolving.
        game_defaults: Default Ren'Py variable values used for static condition
            evaluation (e.g. ``{"month": 6, "day": 1}``).
        rpy_files: Scripts to scan during compose, relative to ``game_root``.
            When set (from the decompile-generated config.json), compose only
            parses these .rpy files instead of sweeping the whole game folder.
        ren_py_files: ``_ren.py`` scripts to scan, relative to ``game_root``.
        output_dir: Directory where generated composite PNGs are written.
            Defaults to ``<game_root>/composites``.
        manifest_path: Path to the manifest JSON produced by the compose pass.
            Defaults to ``<output_dir>/reports/manifest.json``.
        declaration_path: Path to ``composite_images.rpy`` written by the apply
            pass (inside the live ``game`` folder).
        report_path: Path to the adoption report JSON written by the apply pass.
            Defaults to ``<output_dir>/reports/adoption_report.json``.
    """

    name: str
    game_root: Path
    archive_dir: Path
    categories: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_CATEGORY_FOLDERS))
    non_character_folders: frozenset[str] = field(
        default_factory=lambda: frozenset(DEFAULT_NON_CHARACTER_FOLDERS)
    )
    path_aliases: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PATH_ALIASES))
    game_defaults: dict[str, object] = field(default_factory=lambda: dict(DEFAULT_GAME_DEFAULTS))
    rpy_files: list[str] | None = None
    ren_py_files: list[str] | None = None
    script_exclusion_patterns: list[re.Pattern[str]] = field(
        default_factory=list, compare=False
    )
    image_exclusion_patterns: list[re.Pattern[str]] = field(
        default_factory=list, compare=False
    )
    output_dir: Path | None = None
    manifest_path: Path | None = None
    declaration_path: Path | None = None
    report_path: Path | None = None

    # Cached compiled regexes.  Populated once in __post_init__ because
    # GameConfig is frozen and categories never change after construction.
    _path_regex: re.Pattern[str] = field(init=False, compare=False, repr=False)
    _category_folder_regex: re.Pattern[str] = field(init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        """Compile path regexes exactly once per config instance."""
        object.__setattr__(self, "_path_regex", self._compile_path_regex())
        object.__setattr__(self, "_category_folder_regex", self._compile_category_folder_regex())

    # ------------------------------------------------------------------ #
    # Derived helpers
    # ------------------------------------------------------------------ #
    def folder_for(self, category: str) -> str:
        """Return the asset folder name for a canonical category."""
        if category not in CANONICAL_CATEGORIES:
            raise ValueError(f"unknown category {category!r}; expected one of {sorted(CANONICAL_CATEGORIES)}")
        try:
            return self.categories[category]
        except KeyError as exc:
            raise ValueError(
                f"category {category!r} has no folder mapping in config for game {self.name!r}"
            ) from exc

    def category_for_folder(self, folder: str) -> str | None:
        """Return the canonical category for an asset folder name, or None."""
        for category, name in self.categories.items():
            if name == folder:
                return category
        return None

    def is_script_excluded(self, path: str) -> bool:
        """Return True if ``path`` matches any script exclusion pattern."""
        return any(p.search(path) for p in self.script_exclusion_patterns)

    def is_image_excluded(self, source: str) -> bool:
        """Return True if ``source`` matches any image exclusion pattern.

        ``source`` is a forward-slash asset path relative to the archive
        directory (e.g. ``bgs/bgHome.png``).
        """
        return any(p.search(source) for p in self.image_exclusion_patterns)

    def char_dir(self) -> Path:
        return self.archive_dir / self.folder_for("char")

    def bg_dir(self) -> Path:
        return self.archive_dir / self.folder_for("bg")

    def gui_dir(self) -> Path:
        return self.archive_dir / self.folder_for("gui")

    def scene_dir(self) -> Path:
        return self.archive_dir / self.folder_for("scene")

    def asset_path(self, category: str, *rest: str) -> Path:
        """Build an absolute path under a category's asset folder."""
        return self.archive_dir / self.folder_for(category) / Path(*rest)

    def _compile_path_regex(self) -> re.Pattern[str]:
        folders = sorted({name.lower() for name in self.categories.values()})
        alternation = "|".join(re.escape(f) for f in folders)
        pattern = (
            rf"(?:{alternation})/[^\"'\s,)]+\.(?:png|jpe?g|webp|bmp)"
        )
        return re.compile(pattern, re.IGNORECASE)

    def _compile_category_folder_regex(self) -> re.Pattern[str]:
        folders = sorted({name.lower() for name in self.categories.values()})
        alternation = "|".join(re.escape(f) for f in folders)
        return re.compile(rf"(?:{alternation})/", re.IGNORECASE)

    def path_regex(self) -> re.Pattern[str]:
        """Return the compiled regex matching asset paths under any configured folder."""
        return self._path_regex

    def category_folder_regex(self) -> re.Pattern[str]:
        """Return the compiled regex matching ``<folder>/`` prefixes for any configured folder."""
        return self._category_folder_regex

    def resolved_paths(self) -> dict[str, Path]:
        """Convenience: all important absolute paths.

        Output locations default to ``<game_root>/composites`` (i.e.
        ``<game_folder>/composites``) when not explicitly set.
        """
        out = self.output_dir or (self.game_root / "composites")
        return {
            "output_dir": out,
            "manifest_path": self.manifest_path or (out / "reports" / "manifest.json"),
            "declaration_path": self.declaration_path or (self.game_root / "composite_images.rpy"),
            "report_path": self.report_path or (out / "reports" / "adoption_report.json"),
        }

    def to_dict(self) -> dict[str, object]:
        """Serialise the config to a JSON-friendly dict (paths as strings)."""
        paths = self.resolved_paths()
        return {
            "name": self.name,
            "game_root": str(self.game_root),
            "archive_dir": str(self.archive_dir),
            "categories": dict(self.categories),
            "non_character_folders": sorted(self.non_character_folders),
            "path_aliases": dict(self.path_aliases),
            "game_defaults": self.game_defaults,
            "rpy_files": self.rpy_files,
            "ren_py_files": self.ren_py_files,
            "output_dir": str(paths["output_dir"]),
            "manifest_path": str(paths["manifest_path"]),
            "declaration_path": str(paths["declaration_path"]),
            "report_path": str(paths["report_path"]),
        }


def _resolve_project(value: str) -> Path:
    """Resolve a path string against the project root.

    Absolute paths are returned as-is.  Relative paths are joined with
    :data:`PROJECT_ROOT` (the repository root containing ``src/``).
    """
    p = Path(value)
    return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()


def load_config(config_path: str | Path) -> GameConfig:
    """Load a :class:`GameConfig` from the decompile-generated ``config.json``.

    The file must use the decompile schema: ``game_folder`` plus
    ``image_categories`` (as written by :mod:`converter.decompile.config`).
    Paths in the JSON are relative to the project root.

    Output locations are not stored in the JSON; they default to
    ``<game_folder>/composites/...`` via :meth:`GameConfig.resolved_paths`.
    """
    config_path = Path(config_path)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path

    data = json.loads(config_path.read_text(encoding="utf-8"))

    if "game_folder" not in data:
        raise ConfigError(
            f"config.json at {config_path} is missing 'game_folder'; "
            "run decompile first to generate it."
        )

    game_folder = _resolve_project(str(data["game_folder"]))
    archive_dir = game_folder / "archive"

    # image_categories: {"classification_method": "...",
    #                    "image_categories": {"bg": "bgs", "char": "chars", ...}}
    cat_wrapper = data.get("image_categories") or {}
    cat_map_raw: dict[str, str] = {}
    if isinstance(cat_wrapper, dict):
        inner = cat_wrapper.get("image_categories")
        if isinstance(inner, dict):
            cat_map_raw = {str(k): str(v) for k, v in inner.items()}

    categories: dict[str, str] = {}
    for category in CANONICAL_CATEGORIES:
        if category in cat_map_raw:
            folder = cat_map_raw[category]
            # Strip leading "archive/" prefix (decompile records paths relative
            # to the game folder, but categories are relative to archive_dir).
            if folder.lower().startswith("archive/"):
                folder = folder[len("archive/"):]
            categories[category] = folder

    if not categories:
        categories = dict(DEFAULT_CATEGORY_FOLDERS)

    rpy_files = [str(p) for p in (data.get("rpy_files") or [])]
    ren_py_files = [str(p) for p in (data.get("ren_py_files") or [])]

    # --- Script exclusions -------------------------------------------------- #
    # Remove any entry from rpy_files/ren_py_files that matches a script
    # exclusion pattern.  The remaining list is used as the include set by
    # parser.files() and character_associator.
    exc = data.get("exclusion") or {}
    script_exc = exc.get("script") or {}
    script_patterns_raw = script_exc.get("patterns") or []
    script_exclusion_patterns = [re.compile(p) for p in script_patterns_raw]

    if script_exclusion_patterns:
        rpy_files = [
            p for p in rpy_files if not any(x.search(p) for x in script_exclusion_patterns)
        ]
        ren_py_files = [
            p for p in ren_py_files if not any(x.search(p) for x in script_exclusion_patterns)
        ]

    # --- Image exclusions --------------------------------------------------- #
    # Patterns matched against image source paths downstream in the compose
    # pass (record._normalize_layers).  Stored compiled on the config.
    image_exc = exc.get("image") or {}
    image_patterns_raw = image_exc.get("patterns") or []
    image_exclusion_patterns = [re.compile(p) for p in image_patterns_raw]

    return GameConfig(
        name=str(data.get("source_game") or config_path.stem),
        game_root=game_folder,
        archive_dir=archive_dir,
        categories=categories,
        rpy_files=rpy_files,
        ren_py_files=ren_py_files,
        script_exclusion_patterns=script_exclusion_patterns,
        image_exclusion_patterns=image_exclusion_patterns,
    )
