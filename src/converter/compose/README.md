# src\converter\compose\ - Ren'Py Composite Compose Subpackage

## Overview

This subpackage turns a Ren'Py game's 2D assets into reviewable, photorealistic-ready
composites and adopts them back into the game's live scripts.

Every Ren'Py game is modelled as four canonical component categories:

| Category | Meaning           | Default folder (sample game) |
|----------|-------------------|------------------------------|
| ``bg``   | Backgrounds       | ``bgs``                      |
| ``char`` | Characters / sprites | ``chars``                  |
| ``scene``| Scenes / CGs      | ``xxx``                      |
| ``gui``  | User interface    | ``gui``                      |

The mapping from canonical category to on-disk folder is **game-specific** and is
captured in a :class:`GameConfig`.  Nothing about a particular game is hard-coded
in the compose or apply logic.

The flow is:

1. **Compose** (`composer.py`): statically scans the game's indexed ``.rpy``
   scripts for ``layeredimage`` blocks, ``screen`` blocks, and ``image``
   declarations that reference asset folders.  Resolves literal paths and
   conditions, composites the referenced PNGs into a single image per unique
   stack, and writes per-category manifests under ``<output_dir>/reports/``.
2. **Apply** (`applier.py`): reads the manifest, rewrites ``show``/``scene``
   statements in live scripts to point at generated composite tags (or emits
   runtime ``ConditionSwitch`` wrappers when state is dynamic), declares every
   generated composite in ``game/composite_images.rpy``, and writes an adoption
   report.

All modes are config-driven via ``src/converter/config.json`` (produced by the
decompile subpackage).  No ``--game`` lookup is needed.

---

## Module responsibility division

| Module | Responsibility |
|--------|---------------|
| ``config.py`` | :class:`GameConfig` dataclass + :func:`load_config` JSON loader |
| ``models.py`` | Typed domain models (Layer, CompositeRecord, CompositeManifest, AdoptionReport, …) |
| ``errors.py`` | Custom exception hierarchy |
| ``parser.py`` | Ren'Py script parsing (blocks, layeredimage, screen, image declarations) |
| ``resolver.py`` | Asset path resolution (aliases, archive lookup) |
| ``conditions.py`` | Static condition evaluation, call-state reconstruction, interprocedural label states |
| ``compositor.py`` | Image compositing (CPU via PIL) |
| ``character_associator.py`` | Character inference for XXX (scene/adult) composites |
| ``composer.py`` | Compose pass orchestration |
| ``applier.py`` | Apply pass orchestration (script rewriting, declarations, report) |
| ``manifest.py`` | Manifest read/write (per-category + index) |
| ``emitter.py`` | Composite emission and deduplication |
| ``record.py`` | Composite record construction helpers |
| ``regexp.py`` | Compiled regular expressions shared across the subpackage |
| ``cli.py`` | Command-line entry point |
| ``__main__.py`` | ``python -m converter.compose`` support |

The ``restore`` capability (revert modified ``.rpy`` files from ``archive.rpa``)
lives in the sibling ``converter/restore`` subpackage, not inside compose.

---

## Configuration

A :class:`GameConfig` describes one game.  It is normally **not** written by hand:
it is loaded from the decompile-generated ``src/converter/config.json`` via
:func:`converter.compose.config.load_config`:

```json
{
  "schema": 1,
  "game_folder": "src\\renpy_game\\CampMourningWood-0.25.0.1-pc\\game",
  "source_game": "CampMourningWood-0.25.0.1-pc",
  "source_path": "src\\renpy_game\\CampMourningWood-0.25.0.1-pc",
  "rpy_files": ["cabins.rpy", "camp.rpy", "script.rpy", "xxx.rpy", "..."],
  "ren_py_files": [],
  "extracted_archives": [
    { "archive": "archive.rpa", "extracted_folder": "archive" }
  ],
  "image_categories": {
    "classification_method": "folder_based",
    "image_categories": {
      "bg": "archive/bgs",
      "char": "archive/chars",
      "gui": "archive/gui",
      "scene": "archive/xxx"
    }
  },
  "exclusion": {
    "script": {
      "patterns": ["archive/"],
      "note": "Patterns matched against rpy_files/ren_py_files entries"
    },
    "image": {
      "patterns": [],
      "note": "Patterns matched against image source paths"
    }
  }
}
```

Paths in ``config.json`` are relative to the project root (the directory
containing ``src/``).  ``game_folder`` is resolved against the project root, and
``archive_dir`` is always ``game_folder / archive``.  Output locations are not
stored in the JSON; they default to ``<game_root>/composites/...`` via
:meth:`GameConfig.resolved_paths`.

### :class:`GameConfig` fields

- ``name`` — human-readable game identifier (from ``source_game`` in config.json).
- ``game_root`` — the Ren'Py ``\game`` folder (contains the ``.rpy`` scripts).
- ``archive_dir`` — extracted asset archive (always ``game_root / archive``).
- ``categories`` — canonical category -> asset folder name (``bg``, ``char``,
  ``scene``, ``gui``).  Defaults: ``{"bg": "bgs", "char": "chars",
  "scene": "xxx", "gui": "gui"}``.
- ``non_character_folders`` — subfolders under the char dir that aren't characters.
- ``path_aliases`` — asset-path redirects applied when resolving sources.
- ``game_defaults`` — default Ren'Py variable values for static condition eval
  (e.g. ``{"month": 6, "day": 1}``).
- ``rpy_files`` / ``ren_py_files`` — scripts to scan during compose (from config.json).
- ``script_exclusion_patterns`` / ``image_exclusion_patterns`` — compiled regex
  filters from the ``exclusion`` block of config.json.
- ``output_dir`` — where composite PNGs are written (default:
  ``<game_root>/composites``).
- ``manifest_path`` / ``declaration_path`` / ``report_path`` — output locations
  (default to ``<output_dir>/reports/`` and ``<game_root>/composite_images.rpy``).

Use :meth:`GameConfig.resolved_paths` to get all resolved absolute output paths.

---

## Usage

### Via CLI

```bash
# Defaults to src/converter/config.json (produced by decompile)
python -m converter.compose --config src/converter/config.json

# Explicit output directory
python -m converter.compose --config src/converter/config.json --output-dir "composites"

# Custom manifest path
python -m converter.compose --config src/converter/config.json --manifest "composites/reports/manifest.json"

# Dry run (no PNGs or manifest written, apply skipped)
python -m converter.compose --config src/converter/config.json --dry-run

# Remove prior generated PNGs and manifest before rendering
python -m converter.compose --config src/converter/config.json --clean-output

# Compose pass only (skip script adoption)
python -m converter.compose --config src/converter/config.json --compose-only

# Apply pass only (requires an existing manifest)
python -m converter.compose --config src/converter/config.json --apply-only

# Restore .rpy files touched by the apply pass (per manifest)
python -m converter.compose --config src/converter/config.json --restore-compose
```

All CLI modes are also available through :mod:`converter.pipeline` (which adds
the decompile stage and exclusion prompts):

```bash
uv run python -m converter.pipeline                 # decompile -> exclusions -> compose (apply deferred)
uv run python -m converter.pipeline --compose-only   # compose only (skip decompile)
uv run python -m converter.pipeline --apply-only     # apply only (needs manifest)
uv run python -m converter.pipeline --dry-run        # compose without writing
uv run python -m converter.pipeline --restore-compose # undo compose/apply (.rpy from archive)
```

Or via mise tasks:

```bash
mise run pipeline         # default flow: decompile + exclusions + compose
mise run compose          # compose only (uses src/converter/config.json)
mise run compose-clean    # compose only with --clean-output
mise run apply            # apply only (requires manifest)
mise run restore-compose  # undo compose/apply (.rpy from archive)
```

### Via Python import

```python
from converter.compose.config import GameConfig, load_config
from converter.compose.composer import Composer
from converter.compose.manifest import ManifestWriter
from converter.compose.applier import Applier

# 1. Load the decompile-generated config
config = load_config("src/converter/config.json")

# 2. Compose → manifest + PNGs
composer = Composer(config)
manifest = composer.compose(dry_run=False, clean_output=False)
ManifestWriter(config).write(manifest, dry_run=False)

# 3. Apply → adopt composites into live scripts
applier = Applier(config)
manifest, report = applier.apply()
```

### Via Python import (restore)

```python
from converter.restore import restore_rpy_files, restore_from_manifest

# Restore every .rpy stored in archive.rpa back into game_root
results = restore_rpy_files(config.game_root)

# Restore only the scripts the apply pass touched (per manifest)
results = restore_from_manifest(
    config.game_root,
    config.resolved_paths()["manifest_path"].parent / "manifest_index.json",
)
```

---

## Files

```
compose/
├── __init__.py      # Package exports (public API)
├── __main__.py      # python -m converter.compose support
├── cli.py           # Command-line entry point
├── config.py        # GameConfig dataclass + load_config JSON loader
├── models.py        # Typed domain models
├── errors.py        # Custom exception hierarchy
├── parser.py        # Ren'Py script parsing
├── resolver.py      # Asset path resolution
├── conditions.py    # Static condition evaluation + call-state
├── compositor.py    # Image compositing (CPU via PIL)
├── character_associator.py  # Character inference for XXX composites
├── composer.py      # Compose pass orchestration
├── applier.py       # Apply pass orchestration
├── manifest.py      # Manifest read/write (per-category + index)
├── emitter.py       # Composite emission and deduplication
├── record.py        # Composite record construction helpers
├── regexp.py        # Compiled regular expressions
└── README.md        # This file
```

Restore lives in the sibling package:

```
restore/
├── __init__.py      # Package exports (public API)
├── errors.py        # RestoreError
└── restore.py       # restore_rpy_files, restore_from_manifest, restore_decompile, find_archive
```

---

## Manifest layout

The compose pass does **not** write a single monolithic ``manifest.json``.
Under ``<output_dir>/reports/``:

- ``manifest_bg.json``   — composites in the ``bg`` family
- ``manifest_char.json`` — composites in the ``char`` family
- ``manifest_scene.json`` — composites in the ``scene`` family
- ``manifest_gui.json``  — composites in the ``gui`` family
- ``manifest_other.json`` — composites that don't fit a known family
- ``manifest_index.json`` — global data (summary, dynamic_states, image_declarations,
  dynamic_declarations, unresolved) plus references to each category file.

The apply pass loads the index and merges all category manifests in memory.

---
## Generalization notes

Compared with the original game-specific scripts, all of the following are now
driven by :class:`GameConfig` instead of hard-coded constants:

- Asset folder names per category (``chars``/``xxx``/``bgs``/``gui``).
- The path regex that recognizes asset paths.
- The character asset directory and non-character subfolders.
- Per-game path aliases (renamed sprite sheets).
- The ``characters.rpy`` location for ``*ResetDefault`` state tracking.
- Output, manifest, declaration, and report paths.
- Game-specific defaults (e.g. ``month``, ``day``).
- Script and image exclusion patterns (persisted in ``config.json``).

Supporting multiple folders per category (rather than one-to-one) is a natural
extension: ``categories`` can map a category to a list of folders, and
``path_regex()`` / ``category_for_folder()`` would be updated accordingly.

---

## Design principles

- **No module-level mutable global state**: configuration is passed explicitly
  through constructors rather than bound via module-level setters.  This makes
  the code thread-safe and trivial to unit-test.
- **Typed domain models**: raw dicts are replaced with frozen dataclasses
  (:class:`Layer`, :class:`CompositeRecord`, :class:`CompositeManifest`, …)
  with explicit ``replace()`` for immutable updates.
- **Single responsibility**: each module owns exactly one concern (parsing,
  resolution, conditions, compositing, character association, orchestration,
  manifest I/O).
- **Strategy pattern**: compositing uses CPU (PIL) through a common
  :class:`CompositorStrategy` protocol.
- **Idempotent apply**: the apply pass refuses to touch a line whose text no
  longer matches the recorded statement, so a second run is a no-op.

---

## 🔧 Troubleshooting

| Issue | Solution |
|-------|----------|
| `config.json not found` | Run decompile first: `mise run decompile` or `python src/converter/decompile/decompile.py` |
| `No archive.rpa found` | Ensure the game's `.rpa` archives have been extracted by decompile |
| `No game folders found` | Place games under `src\renpy_game\<game>\<game>-pc\game\` |
| `ModuleNotFoundError: rpatool` | Run `uv add git+https://github.com/regiellis/rpa-toolkit.git@435f202e7cb9aeeaf6c006cf928d9661f8830e6e` |

---

*Generated for the renpy-photoreal-vn-converter project.*
