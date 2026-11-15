"""Game configuration for the compose subpackage.

Defines :class:`GameConfig`, which describes how a single Ren'Py game maps its
four canonical component categories (bg, char, scene, gui) onto on-disk asset
folders, plus a loader that can read per-game JSON config files or fall back to
sensible conventions.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

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
        archive_dir: Absolute path to the extracted asset archive. Defaults to
            ``game_root / "archive"``.
        categories: Mapping of canonical category -> asset folder name under
            ``archive_dir``.  Must cover all of :data:`CANONICAL_CATEGORIES`.
        non_character_folders: Subfolder names under the character directory
            that are not treated as characters.
        path_aliases: Forward-slash asset paths (relative to ``archive_dir``)
            that should be redirected to an alternate path when resolving.
        game_defaults: Default Ren'Py variable values used for static condition
            evaluation (e.g. ``{"month": 6, "day": 1}``).
        output_dir: Directory where generated composite PNGs are written.
        manifest_path: Path to the manifest JSON produced by the compose pass.
        declaration_path: Path to ``composite_images.rpy`` written by the apply
            pass (inside the live ``game`` folder).
        report_path: Path to the adoption report JSON written by the apply pass.
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
    output_dir: Path | None = None
    manifest_path: Path | None = None
    declaration_path: Path | None = None
    report_path: Path | None = None

    # Cached compiled regexes (Optimization #2).  Populated once in
    # __post_init__ because GameConfig is frozen and categories never change
    # after construction.
    _path_regex: re.Pattern[str] = field(init=False, compare=False, repr=False)
    _category_folder_regex: re.Pattern[str] = field(init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        """Compile path regexes exactly once per config instance.

        ``path_regex()`` used to call ``re.compile`` on every invocation and was
        called once per scanned line during parsing; compiling here removes that
        repeated work entirely.
        """
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
        return re.compile(pattern, re.I)

    def _compile_category_folder_regex(self) -> re.Pattern[str]:
        folders = sorted({name.lower() for name in self.categories.values()})
        alternation = "|".join(re.escape(f) for f in folders)
        return re.compile(rf"(?:{alternation})/", re.I)

    def path_regex(self) -> re.Pattern[str]:
        """Return the compiled regex matching asset paths under any configured folder.

        Matches ``<folder>/<path>.<ext>`` for every configured category folder,
        case-insensitively.  The pattern is compiled once in :meth:`__post_init__`
        (Optimization #2) instead of on every call.
        """
        return self._path_regex

    def category_folder_regex(self) -> re.Pattern[str]:
        """Return the compiled regex matching ``<folder>/`` prefixes for any configured folder.

        Compiled once in :meth:`__post_init__` (Optimization #2).
        """
        return self._category_folder_regex

    def resolved_paths(self) -> dict[str, Path]:
        """Convenience: all important absolute paths, defaulting output locations."""
        out = self.output_dir or (self.game_root.parent / "composites")
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
            "output_dir": str(paths["output_dir"]),
            "manifest_path": str(paths["manifest_path"]),
            "declaration_path": str(paths["declaration_path"]),
            "report_path": str(paths["report_path"]),
        }


def load_config(path: str | Path) -> GameConfig:
    """Load a :class:`GameConfig` from a JSON file.

    The JSON may contain any of the :class:`GameConfig` fields.  Paths are
    interpreted relative to the config file's parent directory unless they are
    absolute.
    """
    config_path = Path(path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    base = config_path.parent.resolve()

    def rel(value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (base / p).resolve()

    categories_raw = data.get("categories", DEFAULT_CATEGORY_FOLDERS)
    categories: dict[str, str] = dict(categories_raw) if isinstance(categories_raw, dict) else dict(DEFAULT_CATEGORY_FOLDERS)

    non_char_raw = data.get("non_character_folders", DEFAULT_NON_CHARACTER_FOLDERS)
    non_char: frozenset[str] = frozenset(non_char_raw) if isinstance(non_char_raw, (list, set, frozenset)) else DEFAULT_NON_CHARACTER_FOLDERS

    aliases_raw = data.get("path_aliases", DEFAULT_PATH_ALIASES)
    aliases: dict[str, str] = dict(aliases_raw) if isinstance(aliases_raw, dict) else dict(DEFAULT_PATH_ALIASES)

    defaults_raw = data.get("game_defaults", DEFAULT_GAME_DEFAULTS)
    defaults: dict[str, object] = dict(defaults_raw) if isinstance(defaults_raw, dict) else dict(DEFAULT_GAME_DEFAULTS)

    return GameConfig(
        name=str(data.get("name", config_path.stem)),
        game_root=rel(str(data["game_root"])),
        archive_dir=rel(str(data.get("archive_dir", str(rel(str(data["game_root"])) / "archive")))),
        categories=categories,
        non_character_folders=non_char,
        path_aliases=aliases,
        game_defaults=defaults,
        output_dir=rel(str(data["output_dir"])) if "output_dir" in data else None,
        manifest_path=rel(str(data["manifest_path"])) if "manifest_path" in data else None,
        declaration_path=rel(str(data["declaration_path"])) if "declaration_path" in data else None,
        report_path=rel(str(data["report_path"])) if "report_path" in data else None,
    )


def default_config(
    name: str,
    game_root: str | Path,
    archive_dir: str | Path | None = None,
    categories: Mapping[str, str] | None = None,
    **overrides: object,
) -> GameConfig:
    """Build a :class:`GameConfig` from conventions, suitable for a new game.

    ``categories`` defaults to :data:`DEFAULT_CATEGORY_FOLDERS` (bg->bgs,
    char->chars, scene->xxx, gui->gui).  Output locations default to
    ``<game_root_parent>/composites/...``.
    """
    game_root = Path(game_root).resolve()
    if archive_dir is None:
        archive_dir = game_root / "archive"
    else:
        archive_dir = Path(archive_dir).resolve()

    paths: dict[str, Path | None] = {"output_dir": None, "manifest_path": None, "declaration_path": None, "report_path": None}
    tmp = GameConfig(
        name=name,
        game_root=game_root,
        archive_dir=archive_dir,
        categories=dict(categories or DEFAULT_CATEGORY_FOLDERS),
    )
    resolved = tmp.resolved_paths()
    paths["output_dir"] = resolved["output_dir"]
    paths["manifest_path"] = resolved["manifest_path"]
    paths["declaration_path"] = resolved["declaration_path"]
    paths["report_path"] = resolved["report_path"]

    valid_fields = set(GameConfig.__dataclass_fields__)
    kwargs: dict[str, object] = {}
    for key, value in overrides.items():
        if key not in valid_fields:
            continue
        if key == "non_character_folders" and not isinstance(value, frozenset):
            value = frozenset(value)  # type: ignore[arg-type]  (validated override value)
        kwargs[key] = value

    cfg = GameConfig(
        name=name,
        game_root=game_root,
        archive_dir=archive_dir,
        categories=dict(categories or DEFAULT_CATEGORY_FOLDERS),
        output_dir=paths["output_dir"],
        manifest_path=paths["manifest_path"],
        declaration_path=paths["declaration_path"],
        report_path=paths["report_path"],
        **kwargs,  # type: ignore[arg-type]  (overrides are validated against GameConfig fields above)
    )

    # Persist the resolved config so the user can review the exact values being
    # used when default_config() is chosen (no explicit --config JSON).
    try:
        config_json_path = cfg.resolved_paths()["output_dir"] / "config.json"
        config_json_path.parent.mkdir(parents=True, exist_ok=True)
        config_json_path.write_text(json.dumps(cfg.to_dict(), indent=2), encoding="utf-8")
    except OSError:
        # Best-effort: a missing config file must not abort the compose pass.
        pass

    return cfg
