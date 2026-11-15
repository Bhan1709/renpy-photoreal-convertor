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

1. **Compose** (`compose_script_elements.py`): statically scans the game's
   ``.rpy`` scripts for ``layeredimage`` blocks, ``screen`` blocks, and ``image``
   declarations that reference asset folders.  Resolves literal paths and
   conditions, composites the referenced PNGs into a single image per unique
   stack, and writes ``composites/reports/manifest.json``.

2. **Apply** (`apply_composite_tags.py`): reads the manifest, rewrites
   ``show``/``scene`` statements in live scripts to point at generated composite
   tags (or emits runtime ``ConditionSwitch`` wrappers when state is dynamic),
   and declares every generated composite in ``game/composite_images.rpy``.

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
python -m converter.compose.cli --game CampMourningWood-0.25.0.1

# Explicit game root
python -m converter.compose.cli \
  --game-root "src/renpy_game/MyGame/MyGame-pc/game" \
  --output-dir "composites"

# From a JSON config file
python -m converter.compose.cli --config mygame/config.json

# Dry run (no PNGs or manifest written)
python -m converter.compose.cli --game CampMourningWood-0.25.0.1 --dry-run

# Compose pass only (skip script adoption)
python -m converter.compose.cli --game CampMourningWood-0.25.0.1 --compose-only

# Apply pass only (requires an existing manifest)
python -m converter.compose.cli --game CampMourningWood-0.25.0.1 --apply-only
```

### Via Python import

```python
from converter.compose.config import default_config
from converter.compose.compose_script_elements import compose, write_manifest
from converter.compose.apply_composite_tags import apply

config = default_config(
    "MyGame",
    Path("src/renpy_game/MyGame/MyGame-pc/game"),
)

# 1. Compose
manifest = compose(config, dry_run=False)
write_manifest(manifest, config)

# 2. Apply (adopt composites into live scripts)
apply(config)
```

## Files

```
compose/
├── __init__.py      # Package exports (GameConfig, load_config, default_config)
├── config.py        # GameConfig dataclass + JSON loader
├── compose_script_elements.py   # Compose pass: manifest + PNGs
├── apply_composite_tags.py      # Apply pass: script adoption + declarations
├── cli.py           # Command-line entry point
└── README.md        # This file
```

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
