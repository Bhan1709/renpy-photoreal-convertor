# src\converter\decompile\ - Ren'Py Decompile & Extract Subpackage

## Overview

This subpackage provides tools to **decompile** Ren'Py compiled script files
(`.rpyc → .rpy`) and **extract** Ren'Py archive files (`.rpa`) within your
visual novel converter project.

Ren'Py games come in all shapes and sizes, but they all ship a `\game`
folder. The generalized flow:

1. **Find** the first `\game` folder under `src/renpy_game`.
2. **Copy** relevant files (`.rpa`, `.rpyc`, `.rpy`, `_ren.py`) into a clean
   consolidated `src/renpy_game/game` folder, preserving relative structure.
3. **Extract** every `.rpa` alongside itself (`game/archive.rpa → game/archive/`).
4. **Decompile** every `.rpyc` into a sibling `.rpy`.
5. **Detect image categories** (`bg`, `gui`, `char`, `scene`) by folder name or
   Pillow color mode, and store the naming map in `src/converter/config.json`.
6. **Write config.json** with matching patterns, extracted archives, image
   categories, plus an `exclusion` block for downstream filtering.

It can be used both as a CLI tool and as importable Python functions.

---

## 📦 Installation

The subpackage is already included in your project via `rpa-toolkit`:

```bash
# Already done via uv add in your pyproject.toml:
# "rpa-toolkit @ git+https://github.com/regiellis/rpa-toolkit.git@435f202e7cb9aeeaf6c006cf928d9661f8830e6e"

# Verify it's installed:
uv pip list | grep rpa
# → rpa-toolkit 3.0.0

# Or install fresh:
uv add git+https://github.com/regiellis/rpa-toolkit.git@435f202e7cb9aeeaf6c006cf928d9661f8830e6e
```

---

## 📁 Project Structure

```
src/
├── converter/
│   ├── __init__.py          # Entry point (main())
│   └── decompile/           # ← This subpackage
│   │   ├── __init__.py      # Package init + public API
│   │   ├── decompile.py     # Backward-compatible shim + CLI entry point
│   │   ├── config.py        # write_config, load_config
│   │   ├── constants.py     # Paths, extensions, category folder patterns
│   │   ├── decompile_files.py  # decompile_rpyc_files (via rpa-toolkit)
│   │   ├── discovery.py     # find_game_folder, find_game_folders
│   │   ├── exclusions.py    # matches_exclusion, filter_source_files, sync_exclusions
│   │   ├── extract.py       # extract_rpa_file, extract_all_rpa
│   │   ├── flow.py          # process_game, process_all_games (end-to-end flow)
│   │   ├── restore.py       # restore_decompile (undo decompile outputs)
│   │   ├── scan.py          # scan_source_files, scan_extracted_archives, scan_image_categories
│   │   ├── staging.py       # copy_game_files
│   │   └── README.md        # ← You are here
│   └── renpy_game/          # User places games here
│       ├── CampMourningWood-0.25.0.1-pc/
│       │   └── game/
│       │       ├── *.rpyc   ← Decompiled to *.rpy
│       │       ├── archive.rpa ← Extracted alongside
│       │       └── ...
│       └── sample_visual_novel/
│           └── game/
│               └── dialogue.rpy
└── pyproject.toml           # Has rpa-toolkit dependency
```

---

## 🚀 Quick Start

### Via CLI

```bash
# Run the full generalized decompile flow for the first game found
python src\converter\decompile\decompile.py

# Or via mise:
mise run decompile
```

### Via Python Import

```python
from src.converter.decompile import process_game, process_all_games

# Process the first game found under src/renpy_game
process_game()

# Or process every game found
process_all_games()
```

### Via mise tasks

```bash
mise run decompile                    # Full generalized flow
mise run exclusions-sync              # Filter config.json matching patterns by exclusion.patterns
```

---

## 📚 API Documentation

### Discovery

#### `find_game_folder(renpy_games=Path("src/renpy_game"))`
Returns the first directory named `game` found under `src/renpy_game`. Skips
the consolidated output folder (`src/renpy_game/game`) on re-runs.

**Returns:** `Path | None`

#### `find_game_folders()`
Backward-compatible wrapper returning a single-element list of
`(game_dir, rel_path, target_folder)` tuples for the first game folder found.

**Returns:** `list[tuple[Path, Path, Path]]`

#### `set_game_folder_rel_path(rel_path)` / `get_game_folder_rel_path()`
Cache / retrieve the relative path of the `\game` folder for other
subpackages.

### Staging

#### `copy_game_files(game_folder, dest=Path("src/renpy_game/game"))`
Copies `.rpa`, `.rpyc`, `.rpy`, and `_ren.py` files from `game_folder` into
`dest`, preserving relative directory structure. `dest` is recreated cleanly.

**Returns:** `list[Path]` of copied destination paths.

### Extraction

#### `extract_rpa_file(rpa_file)`
Extracts a single `.rpa` alongside itself (`game/archive.rpa → game/archive/`).

#### `extract_all_rpa(game_folder)`
Extracts every `.rpa` under `game_folder`.

**Returns:** `list[dict]` with `archive` and `extracted_folder` keys.

### Decompile

#### `decompile_rpyc_files(game_folder)`
Decompiles every `.rpyc` under `game_folder` to a sibling `.rpy` using
`rpa-toolkit`.

**Returns:** `int` — number of files successfully decompiled.

### Scan

#### `scan_source_files(game_folder)`
Returns relative paths of all `.rpy` and `_ren.py` files.

**Returns:** `{"rpy": [...], "ren_py": [...]}`

#### `scan_extracted_archives(game_folder)`
Returns info about extracted archive folders under `game_folder`.

**Returns:** `list[{"archive": str, "extracted_folder": str}]`

#### `scan_image_categories(game_folder)`
Detects image category folders (`bg`, `gui`, `char`, `scene`) by matching
known Ren'Py folder names. Falls back to Pillow color mode (`RGBA → char`,
`RGB → scene`) when no category folders exist.

**Returns:** `{"classification_method": str, "image_categories": {...}}`

### Config

#### `write_config(game_folder, source_game, source_path, extracted_archives=None, image_categories=None, config_path=...)`
Writes `src/converter/config.json` with matching patterns, extracted
archives, image categories, and an `exclusion` block.

#### `load_config(config_path=...)`
Loads `config.json`; returns `{}` if missing or invalid.

### Exclusions

#### `matches_exclusion(path, patterns)`
Returns `True` if `path` matches any exclusion pattern (substring or glob).

#### `filter_source_files(config_path=...)`
Loads `config.json` and returns `rpy_files`/`ren_py_files` minus excluded
entries.

**Returns:** `{"rpy": [...], "ren_py": [...], "extracted_archives": [...]}`

#### `sync_exclusions(config_path=...)`
Removes any `rpy_files`/`ren_py_files` entry matching `exclusion.patterns`
and rewrites `config.json`.

**Returns:** `{"rpy_before", "rpy_after", "ren_py_before", "ren_py_after"}`

### Flow

#### `process_game(renpy_games=..., dest=..., config_path=..., *, dry_run=False)`
Runs the full generalized decompile flow for one game (find → copy → extract
→ decompile → detect categories → write config).

**Returns:** summary dict.

#### `process_all_games()`
Backward-compatible entry point: processes the first game found.

### Restore

#### `restore_decompile(target_folder, *, dry_run=False)`
Undo a previous decompile: removes generated `.rpy` files with a `.rpyc`
sibling, and removes extracted archive directories.

**Returns:** `dict[str, str]` mapping path → status.

---

## 📂 Extraction Directory Structure

`.rpa` files are extracted **alongside** (in the same directory as) the
`.rpa` file, preserving relative path structure:

```
Before extraction:
src\renpy_game\CampMourningWood-0.25.0.1-pc\
  └── CampMourningWood-0.25.0.1-pc\game\archive.rpa

After extraction:
src\renpy_game\CampMourningWood-0.25.0.1-pc\
  └── CampMourningWood-0.25.0.1-pc\game\
      ├── archive.rpa   ← original file preserved
      ├── archive/      ← extracted directory (named after .rpa stem)
      │   ├── bgs/
      │   ├── chars/
      │   ├── gui/
      │   └── xxx/
      ├── audio/
      ├── *.rpy         ← decompiled .rpy files
      └── ...
```

This design makes **path mapping easier** since extracted files retain their
relative positions within the game directory structure.

---

## 🖼️ Image Category Detection

After extracting and decompiling, `scan_image_categories` detects the image
category naming map so downstream processes know which folder to look into
for `char`, `scene`, `bg`, and `gui` assets.

**Folder-based detection** (preferred): matches known Ren'Py folder names
(case-insensitive):

| Category | Folder name patterns |
|---|---|
| `bg` | `bgs`, `bg`, `background`, `backgrounds` |
| `gui` | `gui`, `ui`, `interface` |
| `char` | `chars`, `char`, `character`, `characters` |
| `scene` | `scene`, `scenes`, `xxx` |

If any category folder is found, the naming map is stored with
`classification_method: "folder_based"` and no per-image mode check is
needed.

**Mode-based fallback**: when no category folders exist, each image is
classified by its Pillow color mode (lazy header read, no pixel decode):
`RGBA → char`, `RGB → scene`. Stored with
`classification_method: "mode_based"`.

Image extensions handled: `.png`, `.webp`, `.avif`, `.jpeg`, `.jpg`.

Example config output:

```json
"image_categories": {
  "classification_method": "folder_based",
  "image_categories": {
    "bg": "archive/bgs",
    "char": "archive/chars",
    "gui": "archive/gui",
    "scene": "archive/xxx"
  }
}
```

---

## ⚙️ mise + uv Configuration

### `.mise.toml`

```toml
[tasks.decompile]
description = "Generalized decompile flow for the first game found"
run = "python src/converter/decompile/decompile.py"

[tasks.exclusions-sync]
description = "Filter config.json matching patterns by exclusion.patterns"
run = "python src/converter/decompile/sync_exclusions_cli.py"
```

### `pyproject.toml` (dependencies)

```toml
[project]
dependencies = [
    "fastapi>=0.163.0",
    "httpx>=0.28.1",
    "rpa-toolkit @ git+https://github.com/regiellis/rpa-toolkit.git@435f202e7cb9aeeaf6c006cf928d9661f8830e6e",
    "tiktoken>=0.14.0",
    "uvicorn>=0.54.0",
]
```

---

## 🛠️ Individual Usage Patterns

### Pattern 1: Run the full flow
```python
from src.converter.decompile import process_game

summary = process_game()
print(summary["config"])          # Path to written config.json
print(summary["image_categories"])  # Detected category naming map
```

### Pattern 2: Decompile specific files only
```python
from src.converter.decompile.decompile_files import decompile_rpyc_files
from pathlib import Path

count = decompile_rpyc_files(Path("src/renpy_game/my_game/game"))
print(f"{count} files decompiled")
```

### Pattern 3: Extract a specific .rpa only
```python
from src.converter.decompile.extract import extract_rpa_file
from pathlib import Path

extract_rpa_file(Path("src/renpy_game/my_game/archive.rpa"))
# Extracted to: src/renpy_game/my_game/archive/
```

### Pattern 4: Sync exclusions into config.json
```python
from src.converter.decompile.exclusions import sync_exclusions

# User sets exclusion.patterns manually in config.json first, e.g. ["archive/"]
result = sync_exclusions()
print(f"rpy_files: {result['rpy_before']} -> {result['rpy_after']}")
```

### Pattern 5: Use cached path in other modules
```python
from src.converter.decompile import find_game_folder, get_game_folder_rel_path

game_folder = find_game_folder()
cached = get_game_folder_rel_path()
```

---

## 🔧 Troubleshooting

### Common Issues

| Issue | Solution |
|---|---|
| `No game folders found` | Ensure games are placed in `src\renpy_game\` with `\game` subfolder |
| `Error: The given file is not a valid Ren'Py archive` | Verify the `.rpa` file is a valid Ren'Py RPA v2/v3 format |
| `Failed to decompile: ...` | Some `.rpyc` files may use obfuscation; try with `--try-harder` flag via CLI |
| `ModuleNotFoundError: rpatool` | Run `uv add git+https://github.com/regiellis/rpa-toolkit.git@435f202e7cb9aeeaf6c006cf928d9661f8830e6e` |
| `ModuleNotFoundError: PIL` | Pillow is required for image mode-based classification |

### Debug Mode

Run with verbose output:

```bash
python -m rpatool.cli extract-decompile game.rpa -v
```

Or within Python:

```python
from rpatool.cli import RenPyArchive
with RenPyArchive(Path("archive.rpa"), verbose=True) as rpa:
    print(rpa.list())  # See what's inside
```

---

## 📬 Need Help?

- Check the main project `README.md` for overall setup
- Ensure `uv` and `mise` are properly configured
- Verify games are in the correct `src\renpy_game\<game>\game\` structure
- The `rpa-toolkit` GitHub issues: https://github.com/regiellis/rpa-toolkit

---

*Generated for the renpy-photoreal-vn-converter project.*
