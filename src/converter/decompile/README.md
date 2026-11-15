# src\converter\decompile\ - Ren'Py Decompile & Extract Subpackage

## Overview

This subpackage provides tools to **decompile** Ren'Py compiled script files (`.rpyc` → `.rpy`) and **extract** Ren'Py archive files (`.rpa`) within your visual novel converter project.

It's designed to work alongside your `mise + uv` project setup and can be used both as a CLI tool and as importable Python functions.

---

## 📦 Installation

The subpackage is already included in your project via `rpa-toolkit`:

```bash
# Already done via uv add in your pyproject.toml:
# "rpa-toolkit @ git+https://github.com/regiellis/rpa-toolkit.git@3.0.0"

# Verify it's installed:
uv pip list | grep rpa
# → rpa-toolkit 3.0.0

# Or install fresh:
uv add git+https://github.com/regiellis/rpa-toolkit.git@3.0.0
```

---

## 📁 Project Structure

```
src/
├── converter/
│   ├── __init__.py          # Entry point (main())
│   └── decompile/           # ← This subpackage
│   │   ├── __init__.py      # Package init
│   │   ├── decompile.py     # Core logic (all functions)
│   │   └── README.md        # ← You are here
│   └── renpy_game/          # User places games here
│       ├── CampMourningWood-0.25.0.1/
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
# Decompile all .rpyc and extract .rpa for all games
python src\converter\decompile\decompile.py

# Or via mise (add to .mise.toml):
# [scripts]
# decompile-games = "python src\\converter\\decompile\\decompile.py"
```

### Via Python Import

```python
from src.converter.decompile.decompile import (
    find_game_folders,
    decompile_rpyc_files,
    extract_rpa_file,
    process_all_games,
)

# Find all games and cache relative path
folders = find_game_folders()

# Decompile .rpyc files in first game
if folders:
    game_dir, rel_path, target = folders[0]
    decompile_rpyc_files(target)

    # Extract .rpa files alongside
    rpa_files = list(target.rglob("*.rpa"))
    for rpa in rpa_files:
        extract_rpa_file(rpa)

# Access cached path for other subpackages
cached = get_game_folder_rel_path()
print(f"Game folder: src/{cached}")
```

---

## 📚 API Documentation

### `find_game_folders()`
Finds all game folders under `src\renpy_game`, locates the `\game` subfolder within each, and **caches** the relative path via `set_game_folder_rel_path()`.

**Returns:** List of `(game_dir, rel_path, target_folder)` tuples

**Example:**
```python
from src.converter.decompile.decompile import find_game_folders

folders = find_game_folders()
# folders = [
#     (PosixPath('src/renpy_game/CampMourningWood-0.25.0.1'),
#      PosixPath('renpy_game/CampMourningWood-0.25.0.1/game'),
#      PosixPath('src/renpy_game/CampMourningWood-0.25.0.1/game'))
# ]
```

### `get_game_folder_rel_path()`
Returns the **cached** relative path of the `\game` folder set by `find_game_folders()`.

**Returns:** `Path | None` - e.g., `Path("renpy_game/CampMourningWood-0.25.0.1/game")`

**Usage pattern (for other subpackages):**
```python
# Must call find_game_folders() in decompile module first!
from src.converter.decompile.decompile import get_game_folder_rel_path

relative_path = get_game_folder_rel_path()
if relative_path:
    # Other subpackages can now use this path
    game_dir = f"src/{relative_path}"
```

### `set_game_folder_rel_path(rel_path)`
Caches the relative path of the `\game` folder for reuse by other subpackages.

**Args:**
- `rel_path`: `Path` object relative to project root (src/), e.g., `Path("renpy_game/my_game/game")`

### `decompile_rpyc_files(game_folder)`
Decompiles all `.rpyc` files in the specified game folder to `.rpy` source code using **rpa-toolkit**.

**Args:**
- `game_folder`: `Path` to the `\game` subfolder containing `.rpyc` files

### `decompile_rpyc_file(rpyc_path, output_path)`
Decompiles a single `.rpyc` file.

**From rpa-toolkit:**
```python
from rpatool import decompile_rpyc_file

result = decompile_rpyc_file(
    Path("script.rpyc"),
    output_path=Path("script.rpy")
)
if result.success:
    print(f"Decompiled! {len(result.source)} chars generated")
    # result.source contains the decompiled .rpy code
else:
    print(f"Error: {result.error}")
```

### `extract_rpa_file(rpa_file)`
Extracts a single `.rpa` file into a directory alongside the `.rpa file`, preserving the directory structure for easier mapping.

**Args:**
- `rpa_file`: `Path` to the `.rpa` file to extract

**Example:**
```python
# .rpa at: src/renpy_game/CampMourningWood-0.25.0.1/CampMourningWood-0.25.0.1-pc/game/archive.rpa
# Extracted to: src/renpy_game/CampMourningWood-0.25.0.1/CampMourningWood-0.25.0.1-pc/game/archive/

from src.converter.decompile.decompile import extract_rpa_file

extract_rpa_file(Path("src/renpy_game/CampMourningWood-0.25.0.1/CampMourningWood-0.25.0.1-pc/game/archive.rpa"))
# Creates: .../game/archive/ with all extracted files
```

### `process_all_games()`
Full workflow: finds games, decompiles `.rpyc`, and extracts `.rpa` files.

---

## 📂 Extraction Directory Structure

The `.rpa` files are extracted **alongside** (in the same directory) as the `.rpa file`, preserving the relative path structure:

```
Before extraction:
src\renpy_game\CampMourningWood-0.25.0.1\
  └── CampMourningWood-0.25.0.1-pc\game\archive.rpa

After extraction:
src\renpy_game\CampMourningWood-0.25.0.1\
  └── CampMourningWood-0.25.0.1-pc\game\
      ├── archive.rpa   ← original file preserved
      ├── archive/      ← extracted directory (named after .rpa stem)
      │   ├── file1.dat
      │   ├── file2.png
      │   └── ...
      ├── audio/
      ├── chars/
      ├── *.rpy         ← decompiled .rpy files
      └── ...
```

This design makes **path mapping easier** since extracted files retain their relative positions within the game directory structure.

---

## ⚙️ mise + uv Configuration

### `.mise.toml` (add scripts)

```toml
[scripts]
decompile-games = "python src\\converter\\decompile\\decompile.py"
# Or individual functions could be wrapped similarly
```

### `pyproject.toml` (dependencies)

```toml
[project]
dependencies = [
    "fastapi>=0.163.0",
    "httpx>=0.28.1",
    "rpa-toolkit @ git+https://github.com/regiellis/rpa-toolkit.git@3.0.0",
    "tiktoken>=0.14.0",
    "uvicorn>=0.54.0",
]
```

---

## 🛠️ Individual Usage Patterns

### Pattern 1: Decompile Specific Files Only
```python
from rpatool import decompile_rpyc_file
from pathlib import Path

# Decompile just one file
result = decompile_rpyc_file(
    Path("src/renpy_game/my_game/game/script.rpyc"),
    output_path=Path("src/renpy_game/my_game/game/script.rpy")
)
print("✓" if result.success else f"✗ {result.error}")
```

### Pattern 2: Extract Specific .rpa Only
```python
from src.converter.decompile.decompile import extract_rpa_file
from pathlib import Path

extract_rpa_file(
    Path("src/renpy_game/my_game/archive.rpa")
)
# Extracted to: src/renpy_game/my_game/archive/
```

### Pattern 3: Build Custom Pipeline
```python
from src.converter.decompile.decompile import find_game_folders, decompile_rpyc_files, extract_rpa_file

# Custom: only decompile, don't extract
folders = find_game_folders()
if folders:
    for game_dir, rel_path, target in folders:
        decompile_rpyc_files(target)
        # Skip extraction if desired
```

### Pattern 4: Using Cached Path in Other Modules
```python
# In another subpackage's module:
from src.converter.decompile.decompile import find_game_folders, get_game_folder_rel_path

# Call this in your setup/init:
find_game_folders()  # This caches the path

# Now use it:
cached = get_game_folder_rel_path()
if cached:
    # Process files in the cached directory
    game_dir = f"src/{cached}"
    print(f"Processing game at: {game_dir}")
```

---

## 🔧 Troubleshooting

### Common Issues

| Issue | Solution |
|-------|----------|
| `No game folders found` | Ensure games are placed in `src\renpy_game\` with `\game` subfolder |
| `Error: The given file is not a valid Ren'Py archive` | Verify the `.rpa` file is a valid Ren'Py RPA v2/v3 format |
| `Failed to decompile: ...` | Some `.rpyc` files may use obfuscation; try with `--try-harder` flag via CLI |
| `UnicodeEncodeError` | This is an OpenCode display issue, not a functional problem - the code works correctly |
| `ModuleNotFoundError: rpa_toolkit` | Run `uv add git+https://github.com/regiellis/rpa-toolkit.git@3.0.0` |

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