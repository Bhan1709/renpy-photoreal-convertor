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

## Pipeline

1. **Compose** (`composer.py`): statically scans the game's
   ``.rpy`` scripts for ``layeredimage`` blocks, ``screen`` blocks, and ``image``
   declarations that reference asset folders.  Resolves literal paths and
   conditions, composites the referenced PNGs into a single image per unique
   stack, and writes ``composites/reports/manifest.json``.

2. **Apply** (`applier.py`): reads the manifest, rewrites
   ``show``/``scene`` statements in live scripts to point at generated composite
   tags (or emits runtime ``ConditionSwitch`` wrappers when state is dynamic),
   and declares every generated composite in ``game/composite_images.rpy``.

## Module responsibility division

| Module | Responsibility |
|--------|---------------|
| ``config.py`` | :class:`GameConfig` dataclass + JSON loader |
| ``models.py`` | Typed domain models (Layer, CompositeRecord, CompositeManifest, …) |
| ``errors.py`` | Custom exception hierarchy |
| ``parser.py`` | Ren'Py script parsing (blocks, layeredimage, screen, image declarations) |
| ``resolver.py`` | Asset path resolution (aliases, archive lookup) |
| ``conditions.py`` | Static condition evaluation, call-state reconstruction |
| ``compositor.py`` | Image compositing (CPU via PIL) |
| ``character_associator.py`` | Character inference for XXX (scene/adult) composites |
| ``composer.py`` | Compose pass orchestration |
| ``applier.py`` | Apply pass orchestration (script rewriting, declarations) |
| ``manifest.py`` | Manifest read/write (full + per-category + index) |
| ``cli.py`` | Command-line entry point |
| ``__main__.py`` | ``python -m converter.compose`` support |

## Configuration

A :class:`GameConfig` describes one game.  It can be supplied as JSON:

```json
{
  "name": "CampMourningWood",
  "game_root": "src/renpy_game/CampMourningWood-0.25.0.1/CampMourningWood-0.25.0.1-pc/game",
  "archive_dir": "src/renpy_game/CampMourningWood-0.25.0.1/CampMourningWood-0.25.0.1-pc/game/archive",
  "categories": {
    "bg": "bgs",
    "char": "chars",
    "scene": "xxx",
    "gui": "gui"
  },
  "non_character_folders": ["fx", "props", "halloween", "war", "dnd", "photos"],
  "path_aliases": {
    "chars/hazel/boobs/boobs1.png": "chars/hazel/boobs/boobs2.png"
  },
  "game_defaults": { "month": 6, "day": 1 },
  "output_dir": "composites",
  "manifest_path": "composites/reports/manifest.json",
  "declaration_path": "game/composite_images.rpy",
  "report_path": "composites/reports/adoption_report.json"
}
```

Paths are relative to the config file's parent directory unless absolute.

### Fields

- ``name`` — human-readable game identifier.
- ``game_root`` — the Ren'Py ``\game`` folder (contains the ``.rpy`` scripts).
- ``archive_dir`` — extracted asset archive (defaults to ``game_root / archive``).
- ``categories`` — canonical category -> asset folder name.  Must cover
  ``bg``, ``char``, ``scene``, ``gui``.
- ``non_character_folders`` — subfolders under the char dir that aren't characters.
- ``path_aliases`` — asset-path redirects applied when resolving sources.
- ``game_defaults`` — default Ren'Py variable values for static condition eval.
- ``output_dir`` — where composite PNGs are written (default:
  ``<game_root_parent>/composites``).
- ``manifest_path`` / ``declaration_path`` / ``report_path`` — output locations.

## Usage

### Via CLI (one command, compose + apply)

```bash
# Defaults to the sample game under src/renpy_game
python -m converter.compose --game CampMourningWood-0.25.0.1

# Explicit game root
python -m converter.compose \
  --game-root "src/renpy_game/MyGame/MyGame-pc/game" \
  --output-dir "composites"

# From a JSON config file
python -m converter.compose --config mygame/config.json

# Dry run (no PNGs or manifest written, apply skipped)
python -m converter.compose --game CampMourningWood-0.25.0.1 --dry-run

# Compose pass only (skip script adoption)
python -m converter.compose --game CampMourningWood-0.25.0.1 --compose-only

# Apply pass only (requires an existing manifest)
python -m converter.compose --game CampMourningWood-0.25.0.1 --apply-only
```

### Via Python import

```python
from pathlib import Path
from converter.compose.config import default_config
from converter.compose.composer import Composer
from converter.compose.manifest import ManifestWriter
from converter.compose.applier import Applier

config = default_config(
    "MyGame",
    Path("src/renpy_game/MyGame/MyGame-pc/game"),
)

# 1. Compose → manifest + PNGs
composer = Composer(config)
manifest = composer.compose(dry_run=False)
ManifestWriter(config).write(manifest)

# 2. Apply → adopt composites into live scripts
applier = Applier(config)
manifest, report = applier.apply()
```

## Files

```
compose/
├── __init__.py      # Package exports (public API)
├── __main__.py      # python -m converter.compose support
├── cli.py           # Command-line entry point
├── config.py        # GameConfig dataclass + JSON loader
├── models.py        # Typed domain models
├── errors.py        # Custom exception hierarchy
├── parser.py        # Ren'Py script parsing
├── resolver.py      # Asset path resolution
├── conditions.py    # Static condition evaluation + call-state
├── compositor.py    # Image compositing (CPU via PIL)
├── character_associator.py  # Character inference for XXX composites
├── composer.py      # Compose pass orchestration
├── applier.py       # Apply pass orchestration
├── manifest.py      # Manifest read/write
├── restore.py       # Restore .rpy files from archive.rpa
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
- The ``PATH_RE`` regex that recognizes asset paths.
- The character asset directory and non-character subfolders.
- Per-game path aliases (renamed sprite sheets).
- The ``characters.rpy`` location for ``*ResetDefault`` state tracking.
- Output, manifest, declaration, and report paths.
- Game-specific defaults (e.g. ``month``, ``day``).

Supporting multiple folders per category (rather than one-to-one) is a natural
extension: ``categories`` can map a category to a list of folders, and
``path_regex()`` / ``category_for_folder()`` would be updated accordingly.

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

## Restore (revert modified .rpy files)

If the apply pass makes unwanted changes to live scripts, you can restore
``.rpy`` files from the game's ``archive.rpa`` using the restore module:

### Via CLI

```bash
# Dry run: report what would be restored without writing
python -m converter.compose --game MyGame --restore-dry-run

# Restore all .rpy files from archive.rpa
python -m converter.compose --game MyGame --restore

# Restore only files touched by the apply pass (per manifest)
python -m converter.compose --game MyGame --restore-from-manifest

# Restore from a specific archive path
python -m converter.compose --game MyGame --restore --archive-path /abs/path/archive.rpa
```

### Via Python import

```python
from converter.compose.restore import restore_rpy_files, restore_from_manifest

# Restore all .rpy files from archive.rpa
results = restore_rpy_files(config)
for rel, status in results.items():
    print(f"{rel}: {status}")

# Restore only files touched by the apply pass
results = restore_from_manifest(config)

# Dry run (report without writing)
results = restore_rpy_files(config, dry_run=True)
```

The restore reads every ``.rpy`` file stored in ``archive.rpa`` and writes it
back to ``game_root``, overwriting any modifications.  This uses the same
``rpatool`` library that the decompile subpackage uses for extraction.
