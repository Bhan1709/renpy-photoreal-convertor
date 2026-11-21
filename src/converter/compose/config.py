"""Game configuration for the compose subpackage.

Defines :class:`GameConfig`, which describes how a single Ren'Py game maps its
four canonical component categories (bg, char, scene, gui) onto on-disk asset
folders, plus a single loader that reads the decompile-generated
``src/converter/config.json``.

All paths in ``config.json`` are relative to the project root (the directory
containing ``src/``).  ``game_folder`` is resolved against the project root,
and extracted archive roots are derived from ``extracted_archives`` in the
config.  Output locations default to ``<game_folder>/composites/...`` via
:meth:`resolved_paths`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..utility.safe_paths import UnsafePathError
from ..utility.safe_paths import safe_relative_path as _shared_safe_relative_path
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

# Speakers that are always kept in dialog context even when they don't match
# a character folder under ``chars/`` (e.g. narrator, player).
DEFAULT_SPECIAL_SPEAKERS: frozenset[str] = frozenset({"narrator", "you", "y"})

# No hard-coded player folder or player aliases by default.  Speaker-to-folder
# mapping is driven entirely by Ren'Py ``Character()`` declarations and the
# game's own ``config.json`` (``player_folder`` / ``player_aliases``), so the
# compose pipeline stays generalized across VN games.
DEFAULT_PLAYER_FOLDER: str | None = None
DEFAULT_PLAYER_ALIASES: tuple[str, ...] = ()


@dataclass(frozen=True)
class GameConfig:
    """Describes one Ren'Py game's asset layout and compose output locations.

    Attributes:
        name: Human-readable game identifier (used for reports/output naming).
        game_root: Absolute path to the Ren'Py ``game`` folder (containing the
            ``.rpy`` scripts and, typically, an ``archive/`` subdirectory).
        archive_dir: Absolute path to the primary extracted asset archive.
        archive_dirs: Absolute paths to every extracted asset archive.  The
            first entry is the primary archive represented by ``archive_dir``.
        source_path: Absolute path to the bundled Ren'Py project/runtime used
            by the native compose stage.  ``None`` keeps legacy callers
            source-compatible, but native compose requires this value.
        categories: Mapping of canonical category -> asset folder name under
            ``archive_dir``.  Must cover all of :data:`CANONICAL_CATEGORIES`.
        non_character_folders: Subfolder names under the character directory
            that are not treated as characters.
        path_aliases: Forward-slash asset paths (relative to ``archive_dir``)
            that should be redirected to an alternate path when resolving.
        game_defaults: Default Ren'Py variable values used for static condition
            evaluation (e.g. ``{"month": 6, "day": 1}``).
        rpy_files: Scripts recorded by decompile, relative to ``game_root``.
            Kept for configuration compatibility; compose now scans every live
            script under ``game_root`` instead of trusting this list.
        ren_py_files: ``_ren.py`` scripts recorded by decompile, relative to
            ``game_root``. Kept for compatibility only.
        output_dir: Directory where generated composite PNGs are written.
            Defaults to ``<game_root>/composites``.
        report_dir: Directory containing split manifests and compose reports.
            Defaults to ``<output_dir>/reports``.
        manifest_index_path: Path to the split-manifest index JSON.
            Defaults to ``<report_dir>/manifest_index.json``.
        manifest_path: Legacy manifest-path override.  It is retained as a
            compatibility anchor for its parent directory; new code should use
            ``manifest_index_path`` and ``report_dir``.
        declaration_path: Path to ``composite_images.rpy`` written by the apply
            pass (inside the live ``game`` folder).
        report_path: Path to the adoption report JSON written by the apply pass.
            Defaults to ``<report_dir>/adoption_report.json``.
    """

    name: str
    game_root: Path
    archive_dir: Path
    archive_dirs: tuple[Path, ...] = ()
    source_path: Path | None = None
    categories: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_CATEGORY_FOLDERS)
    )
    non_character_folders: frozenset[str] = field(
        default_factory=lambda: frozenset(DEFAULT_NON_CHARACTER_FOLDERS)
    )
    special_speakers: frozenset[str] = field(
        default_factory=lambda: frozenset(DEFAULT_SPECIAL_SPEAKERS)
    )
    player_folder: str | None = None
    player_aliases: tuple[str, ...] = field(default_factory=tuple)
    path_aliases: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_PATH_ALIASES)
    )
    game_defaults: dict[str, object] = field(
        default_factory=lambda: dict(DEFAULT_GAME_DEFAULTS)
    )
    rpy_files: list[str] | None = None
    ren_py_files: list[str] | None = None
    # Optional subset of native compose modes ("cfg", "layered_all",
    # "conditions") to run.  ``None`` runs every mode; large games can set
    # ``["cfg"]`` in config.json to keep a compose pass fast.
    compose_modes: tuple[str, ...] | None = None
    output_dir: Path | None = None
    report_dir: Path | None = None
    manifest_index_path: Path | None = None
    manifest_path: Path | None = None
    declaration_path: Path | None = None
    report_path: Path | None = None

    # Cached compiled regexes.  Populated once in __post_init__ because
    # GameConfig is frozen and categories never change after construction.
    _path_regex: re.Pattern[str] = field(init=False, compare=False, repr=False)
    _category_folder_regex: re.Pattern[str] = field(
        init=False, compare=False, repr=False
    )

    def __post_init__(self) -> None:
        """Compile path regexes and normalize archive roots."""
        if not self.archive_dirs:
            object.__setattr__(self, "archive_dirs", (self.archive_dir,))
        elif self.archive_dir not in self.archive_dirs:
            object.__setattr__(
                self,
                "archive_dirs",
                (self.archive_dir, *[path for path in self.archive_dirs if path != self.archive_dir]),
            )
        object.__setattr__(self, "_path_regex", self._compile_path_regex())
        object.__setattr__(
            self, "_category_folder_regex", self._compile_category_folder_regex()
        )

    # ------------------------------------------------------------------ #
    # Derived helpers
    # ------------------------------------------------------------------ #
    def folder_for(self, category: str) -> str:
        """Return the asset folder name for a canonical category."""
        if category not in CANONICAL_CATEGORIES:
            raise ValueError(
                f"unknown category {category!r}; expected one of {sorted(CANONICAL_CATEGORIES)}"
            )
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
        pattern = rf"(?:{alternation})/[^\"'\s,)]+\.(?:png|jpe?g|webp|bmp)"
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
        """Return all resolved absolute output paths.

        ``manifest_index_path`` and ``report_dir`` are the authoritative split-
        manifest contract.  The legacy ``manifest_path`` setting is accepted
        as a directory anchor so existing game configurations keep working.
        """
        out = self.output_dir or (self.game_root / "composites")

        if self.report_dir is not None:
            report_dir = self.report_dir
        elif self.manifest_index_path is not None:
            report_dir = self.manifest_index_path.parent
        elif self.manifest_path is not None:
            report_dir = self.manifest_path.parent
        else:
            report_dir = out / "reports"

        manifest_index_path = self.manifest_index_path or (
            report_dir / "manifest_index.json"
        )

        return {
            "output_dir": out,
            "report_dir": report_dir,
            "manifest_index_path": manifest_index_path,
            # Legacy key retained for callers that have not migrated yet.
            "manifest_path": self.manifest_path or manifest_index_path,
            "declaration_path": self.declaration_path
            or (self.game_root / "composite_images.rpy"),
            "report_path": self.report_path
            or (report_dir / "adoption_report.json"),
        }

    def to_dict(self) -> dict[str, object]:
        """Serialise the config to a JSON-friendly dict (paths as strings)."""
        paths = self.resolved_paths()
        return {
            "name": self.name,
            "game_root": str(self.game_root),
            "archive_dir": str(self.archive_dir),
            "archive_dirs": [str(path) for path in self.archive_dirs],
            "source_path": str(self.source_path) if self.source_path else None,
            "categories": dict(self.categories),
            "non_character_folders": sorted(self.non_character_folders),
            "special_speakers": sorted(self.special_speakers),
            "player_folder": self.player_folder,
            "player_aliases": list(self.player_aliases),
            "path_aliases": dict(self.path_aliases),
            "game_defaults": self.game_defaults,
            "rpy_files": self.rpy_files,
            "ren_py_files": self.ren_py_files,
            "compose_modes": list(self.compose_modes) if self.compose_modes else None,
            "output_dir": str(paths["output_dir"]),
            "report_dir": str(paths["report_dir"]),
            "manifest_index_path": str(paths["manifest_index_path"]),
            "manifest_path": str(self.manifest_path or paths["manifest_index_path"]),
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


def _safe_relative_path(value: str, *, field_name: str) -> Path:
    """Return a normalized relative path or reject an unsafe value.

    Both native and Windows path syntax are checked so a config authored on
    one platform cannot escape its intended root when consumed on another.
    """
    try:
        return _shared_safe_relative_path(value.strip(), field_name=field_name)
    except UnsafePathError as exc:
        raise ConfigError(f"unsafe {field_name} in config: {value!r}") from exc


def _archive_dirs_from_data(
    data: dict[str, object],
    game_folder: Path,
) -> tuple[Path, ...]:
    """Resolve extracted archive roots recorded in the decompile config."""
    raw_entries = data.get("extracted_archives") or []
    if not isinstance(raw_entries, list):
        raise ConfigError("'extracted_archives' must be a list")

    game_root = game_folder.resolve()
    roots: list[Path] = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            raise ConfigError("each extracted archive entry must be an object")
        folder_value = entry.get("extracted_folder")
        if not isinstance(folder_value, str) or not folder_value.strip():
            raise ConfigError("each extracted archive requires 'extracted_folder'")

        relative = _safe_relative_path(
            folder_value,
            field_name="extracted archive folder",
        )
        resolved = (game_folder / relative).resolve()
        if not resolved.is_relative_to(game_root):
            raise ConfigError(
                f"extracted archive folder escapes the game root: {folder_value!r}"
            )
        if resolved not in roots:
            roots.append(resolved)

    if not roots:
        roots.append((game_folder / "archive").resolve())
    return tuple(roots)


def _category_folders(
    data: dict[str, object],
    game_folder: Path,
    archive_dirs: tuple[Path, ...],
) -> dict[str, str]:
    """Normalize and validate canonical category-to-folder mappings."""
    wrapper = data.get("image_categories") or {}
    if not isinstance(wrapper, dict):
        raise ConfigError("'image_categories' must be an object")

    method = str(wrapper.get("classification_method") or "folder_based")
    if method == "mode_based":
        raise ConfigError(
            "mode-based image classification cannot provide category folders; "
            "configure a folder_based 'image_categories' mapping"
        )

    inner = wrapper.get("image_categories")
    if inner is None:
        inner = {}
    if not isinstance(inner, dict):
        raise ConfigError("'image_categories.image_categories' must be an object")

    prefixes: list[str] = []
    for archive_dir in archive_dirs:
        try:
            relative = archive_dir.relative_to(game_folder.resolve())
        except ValueError:
            continue
        prefix = relative.as_posix()
        if prefix not in (".", "") and prefix not in prefixes:
            prefixes.append(prefix)
    if "archive" not in prefixes:
        prefixes.append("archive")

    categories = dict(DEFAULT_CATEGORY_FOLDERS)
    for category in CANONICAL_CATEGORIES:
        if category not in inner:
            continue
        value = inner[category]
        if not isinstance(value, str):
            raise ConfigError(
                f"category folder for {category!r} must be a string, not "
                f"{type(value).__name__}"
            )
        folder = value.replace("\\", "/").strip()
        for prefix in prefixes:
            if folder.lower().startswith(f"{prefix.lower()}/"):
                folder = folder[len(prefix) + 1 :]
                break
        relative = _safe_relative_path(
            folder,
            field_name=f"category folder for {category!r}",
        )
        categories[category] = relative.as_posix()
    return categories


def _string_tuple(value: object, *, field_name: str) -> tuple[str, ...]:
    """Normalize a config value to a tuple of strings."""
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ConfigError(f"'{field_name}' must be a list of strings")
    if not all(isinstance(item, str) for item in value):
        raise ConfigError(f"'{field_name}' must be a list of strings")
    return tuple(str(item) for item in value)


def _optional_project_path(data: dict[str, object], key: str) -> Path | None:
    """Load an optional path relative to the project root."""
    value = data.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ConfigError(f"'{key}' must be a path string")
    return _resolve_project(value)


def load_config(config_path: str | Path) -> GameConfig:
    """Load a :class:`GameConfig` from the decompile-generated ``config.json``.

    The file must use the decompile schema: ``game_folder`` plus
    ``image_categories`` (as written by :mod:`converter.decompile.config`).
    Paths in the JSON are relative to the project root.

    The loader normalizes category mappings, derives extracted archive roots,
    and validates optional compose settings before an expensive compose run.
    """
    config_path = Path(config_path)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path

    try:
        raw_data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"could not load config.json at {config_path}: {exc}") from exc
    if not isinstance(raw_data, dict):
        raise ConfigError(f"config.json at {config_path} must contain an object")
    data: dict[str, object] = raw_data

    if "game_folder" not in data:
        raise ConfigError(
            f"config.json at {config_path} is missing 'game_folder'; "
            "run decompile first to generate it."
        )

    game_folder = _resolve_project(str(data["game_folder"]))
    archive_dirs = _archive_dirs_from_data(data, game_folder)
    archive_dir = archive_dirs[0]
    categories = _category_folders(data, game_folder, archive_dirs)

    rpy_files = list(
        _string_tuple(data.get("rpy_files"), field_name="rpy_files")
    )
    ren_py_files = list(
        _string_tuple(data.get("ren_py_files"), field_name="ren_py_files")
    )

    compose_modes = _string_tuple(data.get("compose_modes"), field_name="compose_modes")
    valid_modes = {"cfg", "layered_all", "conditions"}
    unknown_modes = sorted(set(compose_modes) - valid_modes)
    if unknown_modes:
        raise ConfigError(f"unknown compose mode(s): {', '.join(unknown_modes)}")

    if "path_aliases" in data:
        path_aliases_value = data.get("path_aliases") or {}
        if not isinstance(path_aliases_value, dict):
            raise ConfigError("'path_aliases' must be an object")
        path_aliases: dict[str, str] = {}
        for source, target in path_aliases_value.items():
            if not isinstance(source, str) or not isinstance(target, str):
                raise ConfigError("'path_aliases' keys and values must be strings")
            source_path = _safe_relative_path(
                source,
                field_name="path_aliases source",
            )
            target_path = _safe_relative_path(
                target,
                field_name="path_aliases target",
            )
            path_aliases[source_path.as_posix()] = target_path.as_posix()
    else:
        path_aliases = dict(DEFAULT_PATH_ALIASES)

    if "game_defaults" in data:
        game_defaults_value = data.get("game_defaults") or {}
        if not isinstance(game_defaults_value, dict):
            raise ConfigError("'game_defaults' must be an object")
        game_defaults = dict(game_defaults_value)
    else:
        game_defaults = dict(DEFAULT_GAME_DEFAULTS)

    if "non_character_folders" in data:
        non_character_folders = frozenset(
            _string_tuple(
                data.get("non_character_folders"),
                field_name="non_character_folders",
            )
        )
    else:
        non_character_folders = DEFAULT_NON_CHARACTER_FOLDERS
    if "special_speakers" in data:
        special_speakers = frozenset(
            _string_tuple(data.get("special_speakers"), field_name="special_speakers")
        )
    else:
        special_speakers = DEFAULT_SPECIAL_SPEAKERS

    player_folder_value = data.get("player_folder")
    if player_folder_value is None:
        player_folder = None
    elif not isinstance(player_folder_value, str):
        raise ConfigError("'player_folder' must be a string or null")
    elif not player_folder_value.strip():
        player_folder = None
    else:
        player_folder = _safe_relative_path(
            player_folder_value,
            field_name="player_folder",
        ).as_posix()

    return GameConfig(
        name=str(data.get("source_game") or config_path.stem),
        game_root=game_folder,
        archive_dir=archive_dir,
        archive_dirs=archive_dirs,
        source_path=_optional_project_path(data, "source_path"),
        categories=categories,
        non_character_folders=non_character_folders,
        special_speakers=special_speakers,
        player_folder=player_folder,
        player_aliases=_string_tuple(
            data.get("player_aliases"), field_name="player_aliases"
        ),
        path_aliases=path_aliases,
        game_defaults=game_defaults,
        rpy_files=rpy_files,
        ren_py_files=ren_py_files,
        compose_modes=compose_modes or None,
        output_dir=_optional_project_path(data, "output_dir"),
        report_dir=_optional_project_path(data, "report_dir"),
        manifest_index_path=_optional_project_path(data, "manifest_index_path"),
        manifest_path=_optional_project_path(data, "manifest_path"),
        declaration_path=_optional_project_path(data, "declaration_path"),
        report_path=_optional_project_path(data, "report_path"),
    )
