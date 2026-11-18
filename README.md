# Ren'Py Photoreal VN Converter

[![AI Ready](https://img.shields.io/badge/AI--Ready-yes-brightgreen?style=flat)](https://github.com/johnpapa/ai-ready)

This project is intended to be a toolkit to convert Ren'Py based games and visual novels from 2D format to photorealistic versions using local Ollama and free open-source models.

## 🎮 Features

- **Decompile** Ren'Py `.rpyc`/`.rpymc` scripts to readable `.rpy` source
- **Extract** Ren'Py `.rpa`/`.rpi` archive files into directory structures
- **Cache** game folder relative paths for use by other converter subpackages
- **Compose** photorealistic composites from game assets and adopt them into live scripts
- **Apply** composite tags into Ren'Py scripts (with runtime `ConditionSwitch` fallbacks)
- **Restore** modified `.rpy` files from archives
- **Integrated** with `mise + uv` project management
- **Extracted files** preserved alongside `.rpa` files for easier path mapping

## 📦 Quick Start

### Prerequisites

- Python 3.14+ (recommended via [mise](https://mise.jdx.dev/) or `pyenv`)
- [Ollama](https://ollama.com/) installed and running locally
- (Optional) NVIDIA API key if you want to use NVIDIA hosted models via the rate‑limit proxy
- Git (for version control)

### Installation

1. **Clone the repository** (or download the source):

   ```bash
   git clone https://github.com/your-org/renpy-photoreal-vn-converter.git
   cd renpy-photoreal-vn-converter
   ```

2. **Install dependencies** (using uv, recommended):

   ```bash
   uv sync
   ```

   Or with `pip`:

   ```bash
   pip install -e .
   ```

3. **Set up Ollama models** (see the `agent/README.md` for detailed Modelfile instructions):

   - Pull or build the desired Ollama models, e.g.:

     ```bash
     ollama pull qwen3-vl-32k
     # or build from a Modelfile:
     ollama create qwen3-vl-32k -f src/agent/Modelfiles/qwen3vl32
     ```

   - Verify the model is available:

     ```bash
     ollama list
     ```

4. **(Optional) Set NVIDIA API key** if using NVIDIA models:

   ```bash
   export NVIDIA_API_KEY=your_key_here
   ```

5. **Run the agent** with a profile of your choice:

   ```bash
   python -m agent <profile>
   ```

   Example:

   ```bash
   python -m agent qwen-3-vl-32k
   ```

   Or via mise:

   ```bash
   mise run agent qwen-3-vl-32k
   mise run agent nvidia-glm
   ```

   This will:
   - Write the profile configuration to `opencode.json` (or `opencode.jsonc`).
   - Start the ConversationMonitor to back up conversation state.
   - Launch OpenCode through the NVIDIA proxy (if applicable) or directly via Ollama.

6. **Decompile and extract Ren'Py games**:

   ```bash
   python src/converter/decompile/decompile.py
   ```

   Or via mise:

   ```bash
   mise run decompile
   ```

   This produces `src/converter/config.json` (the GameConfig used by compose/apply).

7. **Compose photoreal composites** (after decompile):

   ```bash
   mise run compose
   ```

   Compose is the single entry point: it checks exclusion patterns first, then
   offers clean, compose, clean+compose, review, apply, and restore. Restore can be
   run via the compose CLI: `python -m converter.compose restore` (equivalently
   choose "Restore game folder from source" in the interactive menu), which recovers
   the live game folder from the original source while keeping the generated composites.

   Advanced (explicit actions / pipeline flags):

   ```bash
   # Skip the menu with an explicit action
   python -m converter.compose clean
   python -m converter.compose clean-compose
   python -m converter.compose review
   python -m converter.compose apply
   python -m converter.compose restore

   # Full pipeline: decompile → exclusions → compose (apply deferred)
   python -m converter.pipeline
   ```

## 📁 Project Structure

```
.
├── README.md                # this file
├── opencode.json            # OpenCode project config (auto‑generated)
├── pyproject.toml           # package configuration
├── uv.lock                  # lock file for uv
├── .gitignore
├── .python-version
├── mise.toml                # tool version management + task definitions
├── .agents/
│   └── skills/              # git‑commit, context‑map, gh‑attach skill definitions
├── src/
│   ├── agent/               # OpenCode orchestration (top-level `agent` module)
│   │   ├── __init__.py
│   │   ├── __main__.py      # python -m agent <profile>
│   │   ├── cli.py           # profile launcher, proxy, Ollama/NVIDIA integration
│   │   ├── config.json      # bundled model profiles
│   │   ├── config.py        # profile loading/validation
│   │   ├── constants.py     # proxy constants (host, port, etc.)
│   │   ├── monitor.py       # context‑window monitor & backup
│   │   ├── Modelfiles/      # Ollama Modelfiles (qwen3vl128, qwen3vl64, …)
│   │   ├── nvidia_proxy.py  # rate‑limit reverse proxy for NVIDIA APIs
│   │   └── README.md        # agent subpackage documentation
│   ├── converter/           # Ren'Py conversion toolkit (top-level `converter` module)
│   │   ├── __init__.py      # Entry point (main())
│   │   ├── pipeline.py      # End-to-end decompile → compose → apply pipeline
│   │   ├── config.json      # Decompile-generated GameConfig (src/converter/config.json)
│   │   ├── py.typed
│   │   ├── utility/         # Shared utilities (tqdm)
│   │   ├── decompile/       # Ren'Py decompile/extract subpackage
│   │   │   ├── __init__.py
│   │   │   ├── decompile.py # Core logic + CLI entry point
│   │   │   ├── config.py    # write_config, load_config
│   │   │   ├── constants.py
│   │   │   ├── discovery.py # find_game_folder, find_game_folders
│   │   │   ├── exclusions.py
│   │   │   ├── extract.py   # extract_rpa_file
│   │   │   ├── flow.py      # process_game, process_all_games
│   │   │   ├── restore.py   # restore_decompile
│   │   │   ├── scan.py
│   │   │   ├── staging.py
│   │   │   └── README.md    # This subpackage documentation
│   │   ├── compose/         # Compose/review/apply subpackage
│   │   │   ├── __init__.py
│   │   │   ├── cli.py       # Command-line entry point
│   │   │   ├── config.py    # GameConfig + load_config
│   │   │   ├── composer.py  # Compose pass orchestration
│   │   │   ├── applier.py   # Apply pass orchestration
│   │   │   ├── manifest.py  # Per-category manifest read/write
│   │   │   ├── models.py    # Typed domain models
│   │   │   ├── parser.py    # Ren'Py script parsing
│   │   │   ├── resolver.py  # Asset path resolution
│   │   │   ├── conditions.py
│   │   │   ├── compositor.py
│   │   │   ├── character_associator.py
│   │   │   ├── emitter.py
│   │   │   ├── record.py
│   │   │   ├── regexp.py
│   │   │   └── README.md    # This subpackage documentation
│   │   └── restore/         # Restore .rpy files from archive.rpa
│   │       ├── __init__.py
│   │       ├── errors.py
│   │       └── restore.py   # restore_rpy_files, restore_from_manifest
│   └── renpy_game/          # User places Ren'Py games here (gitignored except .gitkeep)
│       ├── CampMourningWood-0.25.0.1-pc/
│       │   └── game/
│       │       ├── *.rpyc   # Compiled scripts (decompiled to *.rpy)
│       │       ├── archive.rpa # Extracted alongside
│       │       └── ...
│       └── sample_visual_novel/
│           └── game/
│               └── dialogue.rpy   # Sample decompiled file
```

## 🔧 Pipeline Overview

1. **Decompile** `.rpyc` files and extract `.rpa` archives using the `src/converter/decompile/` subpackage (writes `src/converter/config.json`)
2. **Create composites** from different components (backgrounds, GUI, characters, scenes) using the `src/converter/compose/` subpackage
3. **Adopt composites** into live Ren'Py scripts (rewrite `show`/`scene` statements, emit `ConditionSwitch` wrappers, write `composite_images.rpy`)
4. **(Optional) Restore** modified `.rpy` files from the archive using the `src/converter/restore/` subpackage

All stages are config-driven via `src/converter/config.json`; none require a `--game` argument.

## 📚 Recent Additions

### Compose Subpackage (`src/converter/compose/`)

New generalized subpackage for building photoreal composites from Ren'Py game assets:

- **`GameConfig` / `load_config`** — Game-specific config loaded from decompile-generated `src/converter/config.json`
- **`Composer.compose()`** — Scans `.rpy` scripts for layeredimage/screen/image declarations, composites referenced PNGs, writes per-category manifests
- **`Applier.apply()`** — Rewrites `show`/`scene` statements to composite tags, emits runtime `ConditionSwitch` wrappers, declares composites in `composite_images.rpy`
- **`ManifestWriter`** — Writes per-category manifests (`manifest_bg.json`, …) + `manifest_index.json`
- **`restore_rpy_files()` / `restore_from_manifest()`** — Revert modified `.rpy` files from `archive.rpa` (in `converter/restore`)

See `src/converter/compose/README.md` for full documentation.

### Decompile Subpackage (`src/converter/decompile/`)

New subpackage for handling Ren'Py script decompilation and RPA archive extraction:

- **`find_game_folders()`** - Discovers all games under `src\renpy_game\`, locates `\game` subfolders, and caches the relative path
- **`get_game_folder_rel_path()` / `set_game_folder_rel_path()`** - Reusable path caching for other subpackages
- **`decompile_rpyc_files()` / `decompile_rpyc_file()`** - Decompile `.rpyc` → `.rpy` using rpa-toolkit
- **`extract_rpa_file()`** - Extract `.rpa` archives into directory alongside the `.rpa` file, preserving relative path structure
- **`process_all_games()`** - Full workflow: find → decompile → extract

Example extraction path structure:
```
.src\renpy_game\CampMourningWood-0.25.0.1-pc\
  └─ CampMourningWood-0.25.0.1-pc\game\
      ├─ archive.rpa          # Original file preserved
      ├─ archive/             # Extracted directory (named after .rpa stem)
      │  ├─ bgs/
      │  ├─ chars/
      │  ├─ gui/
      │  └─ xxx/
      ├─ audio/
      ├─ *.rpy                # Decompiled .rpy files
      └─ ...
```

### Package Management

- **rpa-toolkit** installed via: `uv add git+https://github.com/regiellis/rpa-toolkit.git@435f202e7cb9aeeaf6c006cf928d9661f8830e6e`
- Added to `pyproject.toml` dependencies
- Integrated with `mise` through `mise.toml` scripts section

## 🛠️ Usage Patterns

### Decompile Specific Files

```python
from rpatool import decompile_rpyc_file
from pathlib import Path

result = decompile_rpyc_file(
    Path("src/renpy_game/my_game/game/script.rpyc"),
    output_path=Path("src/renpy_game/my_game/game/script.rpy")
)
```

### Extract Specific .rpa

```python
from src.converter.decompile.decompile import extract_rpa_file
extract_rpa_file(Path("src/renpy_game/my_game/archive.rpa"))
# Extracted to: src/renpy_game/my_game/archive/
```

### Using Cached Path in Other Subpackages

```python
from src.converter.decompile.decompile import find_game_folders, get_game_folder_rel_path

find_game_folders()  # Cache the path
cached = get_game_folder_rel_path()
if cached:
    game_dir = f"src/{cached}"
    # Process files in game_dir
```

### Compose + Apply (via Python import)

```python
from converter.compose.config import load_config
from converter.compose.compose_pass import Composer
from converter.compose.manifest import ManifestWriter
from converter.compose.apply_pass import Applier

config = load_config("src/converter/config.json")

# 1. Compose → manifest + PNGs
manifest = Composer(config).compose(dry_run=False)
ManifestWriter(config).write(manifest, dry_run=False)

# 2. Apply → adopt composites into live scripts
manifest, report = Applier(config).apply()
```

## 🚧 Roadmap

- [x] **Add decompilation utilities** for `.rpyc`/`.rpa` extraction ✅
- [x] **Add compose/apply subpackage** for photoreal composites ✅
- [ ] Integrate ComfyUI local API driver
- [ ] Provide example workflow JSON for ComfyUI
- [ ] Expand Modelfiles for additional context sizes and models
- [ ] Document CI/CD and testing setup

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to raise contributions and open pull requests. All changes must be made on a dedicated branch (Conventional Branch naming) and use Conventional Commits. Before opening a PR, run `uv run ruff` and `uv run mypy src/` and confirm both pass.

Feel free to open issues or contribute new profiles, Modelfiles, or pipeline steps.
